// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.100 (WP-03a, AD-1): ONE orchestrator-update pipeline, ONE command.
//!
//! Before this file, five commands updated the same clone and each was its
//! own program (review L2 §1, D1–D8): one skipped install.py entirely, one
//! degraded to a source-only rebuild AFTER the pull had landed when Python was
//! missing, one never stopped the hub or closed the DB, two relaunched
//! different binaries, and all of them rendered failures as
//! `"Update failed: " + stderr` — empty on the field machine.
//!
//! [`run_update`] is the replacement: every kind runs the SAME thirteen
//! phases, in the SAME order, recorded in a [`PhaseLedger`]:
//!
//! | # | phase | what |
//! |---|---|---|
//! | 1 | `resolve_root` | install root via `install_root` (AD-2), identity-checked, exe inside it |
//! | 2 | `claim` | in-process single-flight + cross-process `<vct_root>/update.lock` |
//! | 3 | `preflight` | git, Python + `install.py`, kind routed, tree state, hub-stop pre-check, remote pin — **before any mutation** |
//! | 4 | `sweep_and_gate` | MCP kill-sweep + update gate — only after every refusal (L2-F14) |
//! | 5 | `hub_stop_and_renames` | stop vct-hub, rename binaries aside (Windows) |
//! | 6 | `git_op` | by kind: fast-forward pull / merge / rebase / resume check / backed-up hard reset / none |
//! | 7 | `head_advance` | HEAD reached upstream (the v0.2.62 crash class) |
//! | 8 | `install_py` | `install.py --update` inside `DbUpdateClosedGuard`, streamed to the modal |
//! | 9 | `db_reopen_and_refresh` | cached update state refreshed |
//! | 10 | `gate_drop_and_hub_restart` | gate dropped BEFORE the binary check; hub restarted (POSIX; Windows after the handoff decision, v0.2.54 C-1) |
//! | 11 | `binary_refresh` | the dist sidecar vs source — ONE read, no `git pull` (L2-F11) |
//! | 12 | `bookkeeping` | shortcut, hardware re-detect flag, module retry sweep, the success audit row — before the restart hop (L2-F12) |
//! | 13 | `relaunch` | `restart::relaunch(Dist)` with the strict version guard |
//!
//! Every failure leg writes `update_orchestrator_complete{success:false}` —
//! from ONE place ([`run_update_with`]), so no leg can forget it — and returns
//! an [`UpdateSurfaceError`] whose wire form is `update_failure`'s contract.
//!
//! The side effects live behind [`UpdateOps`] so the ORDER and the refusal
//! guarantees are unit-tested with a recording fake (`tests` below): a
//! preflight refusal is proven to perform no mutation at all.
//!
//! Every kind is routed (WP-03b): `Merge` / `Rebase` / `Resume` run their
//! git operation from `update_pipeline` (`merge_upstream`,
//! `rebase_onto_upstream`, `classify_resume`); the legacy per-surface
//! commands that used to own them — each with its own tail — are gone.
//! `ResetHard` saves the local commits and the whole working tree (a verified
//! `git bundle` under `<vct_root>/backups/` + `vco-backup/<stamp>` branches)
//! BEFORE any merge/rebase abort and before the reset, refuses the reset when
//! that backup cannot be made ([`create_reset_backup`]) or the abort does not
//! leave a clear tree, and leaves HEAD attached to the update branch
//! ([`reset_hard_git_op`]).

use std::future::Future;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{command, AppHandle, Manager as _, Runtime, Window};

use crate::commands::installer::HubRestartContext;
use crate::commands::restart::{RelaunchError, RelaunchOutcome, RelaunchRefusal};
use crate::commands::update_failure::{self, AuditRows, UpdateSurfaceError};
use crate::commands::update_pipeline::{InstallPyRun, PrePullRenames};

/// The surface name in log lines, audit rows and the ledger.
pub(crate) const SURFACE: &str = "run_orchestrator_update";

/// The version of the running launcher binary.
pub(crate) const RUNNING_VERSION: &str = env!("CARGO_PKG_VERSION");

/// What the user asked for. Serialises as the variant NAME (`"PullFf"`), which
/// is the wire form of the command argument — one enum, no DTO mirror to
/// drift from it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum UpdateKind {
    /// Fast-forward (or conflict-free A0 merge) to the upstream tip.
    PullFf,
    /// Merge upstream into a diverged clone.
    Merge,
    /// Rebase a diverged clone onto upstream.
    Rebase,
    /// Continue an update halted at a resolved conflict.
    Resume,
    /// No git operation: re-apply `install.py --update` to the current tree.
    ApplyOnly,
    /// `git reset --hard vco_upstream/<branch>` — the diverged-clone rescue,
    /// after the local commits are saved ([`create_reset_backup`]).
    ResetHard,
}

/// The command argument's type name in the contract.
pub type UpdateKindDto = UpdateKind;


// ---------------------------------------------------------------------------
// The phase ledger
// ---------------------------------------------------------------------------

/// The thirteen phases, in their one order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Phase {
    ResolveRoot,
    Claim,
    Preflight,
    SweepAndGate,
    HubStopAndRenames,
    GitOp,
    HeadAdvance,
    InstallPy,
    DbReopenAndRefresh,
    GateDropAndHubRestart,
    BinaryRefresh,
    Bookkeeping,
    Relaunch,
}

/// Every phase in execution order (the `Ord` of [`Phase`]; the tests pin the
/// ledger against it).
#[cfg(test)]
pub(crate) const PHASE_ORDER: [Phase; 13] = [
    Phase::ResolveRoot,
    Phase::Claim,
    Phase::Preflight,
    Phase::SweepAndGate,
    Phase::HubStopAndRenames,
    Phase::GitOp,
    Phase::HeadAdvance,
    Phase::InstallPy,
    Phase::DbReopenAndRefresh,
    Phase::GateDropAndHubRestart,
    Phase::BinaryRefresh,
    Phase::Bookkeeping,
    Phase::Relaunch,
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum PhaseStatus {
    Done,
    Skipped,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct PhaseRecord {
    pub phase: Phase,
    pub status: PhaseStatus,
    pub detail: Option<String>,
}

/// What happened, phase by phase. Appends only in [`PHASE_ORDER`]; an
/// out-of-order append is a programming error and panics in debug builds.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub(crate) struct PhaseLedger {
    pub entries: Vec<PhaseRecord>,
}

impl PhaseLedger {
    fn push(&mut self, phase: Phase, status: PhaseStatus, detail: Option<String>) {
        debug_assert!(
            self.entries.last().is_none_or(|last| last.phase < phase),
            "phase {:?} recorded out of order after {:?}",
            phase,
            self.entries.last().map(|l| l.phase)
        );
        self.entries.push(PhaseRecord {
            phase,
            status,
            detail,
        });
    }
    fn done(&mut self, phase: Phase) {
        self.push(phase, PhaseStatus::Done, None);
    }
    fn done_with(&mut self, phase: Phase, detail: impl Into<String>) {
        self.push(phase, PhaseStatus::Done, Some(detail.into()));
    }
    fn skip(&mut self, phase: Phase, why: impl Into<String>) {
        self.push(phase, PhaseStatus::Skipped, Some(why.into()));
    }
    fn fail(&mut self, phase: Phase, why: impl Into<String>) {
        self.push(phase, PhaseStatus::Failed, Some(why.into()));
    }
    /// The phase that failed, if any.
    pub(crate) fn failed_phase(&self) -> Option<Phase> {
        self.entries
            .iter()
            .find(|r| r.status == PhaseStatus::Failed)
            .map(|r| r.phase)
    }
}

/// What a successful update did — the command's `Ok` value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UpdateOutcome {
    pub kind: UpdateKind,
    pub head_before: Option<String>,
    pub head_after: Option<String>,
    pub install_py_ran: bool,
    pub restarted: bool,
    pub log_path: PathBuf,
    /// One human sentence (never empty) — what happened, incl. why the
    /// launcher was not restarted when it was not.
    pub message: String,
    pub(crate) phases: Vec<PhaseRecord>,
    /// `ResetHard` only: where the discarded local commits were saved.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reset_backup: Option<ResetBackup>,
}

/// What `ResetHard` saved before discarding the clone's local work (owner
/// ruling F-W2-03). Named in the confirm dialog (by pattern) and in the
/// result (exactly).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ResetBackup {
    /// `vco-backup/<stamp>` — the pre-reset HEAD.
    pub branch: String,
    /// `vco-backup/<stamp>-wip` — a commit of the whole working tree
    /// (tracked edits and deletions, conflict-marked files, untracked
    /// non-ignored files; the launcher's own `.old-<pid>` rename artefacts
    /// excluded), built in a throwaway index so nothing on disk or in the
    /// user's index changes. `None` when the tree matched HEAD.
    pub uncommitted_branch: Option<String>,
    /// `vco-backup/<stamp>-branch` — the update branch's own tip, when HEAD
    /// was not on it (detached, or mid-rebase) and it held commits neither
    /// HEAD nor upstream has; the reset moves that branch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub branch_tip: Option<String>,
    /// `<vct_root>/backups/orchestrator-reset-<stamp>.bundle` holding every
    /// saved branch's commits that upstream does not have, verified with
    /// `git bundle verify`. `None` only when there was nothing local to save
    /// (no local commit, no uncommitted change).
    pub bundle: Option<PathBuf>,
    /// Commits on HEAD (and on the saved branch tip) that upstream does not
    /// have.
    pub local_commits: u32,
}

impl ResetBackup {
    /// The sentence the result carries.
    pub(crate) fn describe(&self) -> String {
        let mut all = vec![self.branch.as_str()];
        all.extend(self.branch_tip.as_deref());
        all.extend(self.uncommitted_branch.as_deref());
        let refs = match all.split_last() {
            Some((last, rest)) if !rest.is_empty() => format!("{} and {}", rest.join(", "), last),
            _ => self.branch.clone(),
        };
        match &self.bundle {
            Some(b) => format!(
                "Your {} local commit(s){} were saved to {} {} and to {}.",
                self.local_commits,
                if self.uncommitted_branch.is_some() {
                    " and uncommitted changes"
                } else {
                    ""
                },
                if all.len() > 1 { "branches" } else { "branch" },
                refs,
                b.display()
            ),
            None => format!(
                "There were no local commits or uncommitted changes to lose; the pre-reset \
                 HEAD is kept as branch {}.",
                refs
            ),
        }
    }
}

// ---------------------------------------------------------------------------
// The side-effect seam
// ---------------------------------------------------------------------------

/// What the git operation established.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct GitOpOutcome {
    /// Nothing was fetched; install.py is not run.
    pub already_up_to_date: bool,
    /// Only with `already_up_to_date`: the dist binary is newer than the
    /// running one (a relaunch is owed).
    pub dist_binary_stale: bool,
    pub branch: String,
    /// `ResetHard` only: what was saved before the reset.
    pub reset_backup: Option<ResetBackup>,
}

/// A failed git operation. `restored` = the failing leg already reverted the
/// binary renames and restarted the hub (the pull sequence's own abort tail);
/// otherwise the driver does.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct GitOpFailure {
    pub error: UpdateSurfaceError,
    pub restored: bool,
}

/// Phase 11's verdict: is the dist binary the one this update needs?
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum BinaryCheck {
    /// The running launcher is already at or above the source version.
    RunningCurrent,
    /// The dist launcher (and hub, when it has a sidecar) reached the source.
    Ready,
    /// The dist launcher is below the source but newer than the running one —
    /// relaunching into it is strictly better (deferral records the gap).
    Partial {
        running: String,
        dist: String,
        source: String,
    },
    /// The dist launcher reached the source; the dist vct-hub sidecar did not
    /// (a release whose hub build did not land, a hand-copied hub). The
    /// relaunch goes ahead; the note names the hub, and no launcher-divergence
    /// row is written (W2R-02: that row claimed the LAUNCHER was behind).
    HubLagging { hub: String, source: String },
    /// No dist binary newer than the running one exists yet (the release has
    /// not committed it). The launcher keeps running; deferral recorded.
    NotPublished {
        running: String,
        dist: String,
        source: String,
    },
    /// A version could not be read or parsed — not ranked (AD-8 tri-state);
    /// the relaunch guard decides.
    Unknown { reason: String },
}

/// Pure decision for phase 11, from ONE read of the three versions: the
/// source (`vct-module.json`), the dist launcher sidecar and the dist hub
/// sidecar (absent or empty = no hub sidecar, which never blocks). Every
/// comparison goes through the version SSOT; each binary is compared with
/// the SOURCE, so "below the source" is only ever said of a binary that is.
/// v0.2.100 WP-03b: replaces the `WaitForBinaryRefresh` poll (and its
/// `git pull` behind install.py's back, L2-F11).
pub(crate) fn decide_binary_refresh(
    running: &str,
    source: Option<&str>,
    dist: Option<&str>,
    hub: Option<&str>,
) -> BinaryCheck {
    use vct_launcher_core::version::is_older;
    let unknown = |e: vct_launcher_core::version::VersionParseError| BinaryCheck::Unknown {
        reason: e.to_string(),
    };
    let Some(source) = source else {
        return BinaryCheck::Unknown {
            reason: "the source version (vct-module.json) could not be read".into(),
        };
    };
    match is_older(running, source) {
        Err(e) => return unknown(e),
        Ok(false) => return BinaryCheck::RunningCurrent,
        Ok(true) => {}
    }
    let not_published = |dist: &str| BinaryCheck::NotPublished {
        running: running.into(),
        dist: if dist.is_empty() { "<unknown>".into() } else { dist.into() },
        source: source.into(),
    };
    let Some(dist) = dist.filter(|d| !d.trim().is_empty()) else {
        return not_published("");
    };
    match is_older(dist, source) {
        Err(e) => unknown(e),
        // The dist launcher is below the source.
        Ok(true) => match is_older(running, dist) {
            Ok(true) => BinaryCheck::Partial {
                running: running.into(),
                dist: dist.into(),
                source: source.into(),
            },
            Ok(false) => not_published(dist),
            Err(e) => unknown(e),
        },
        // The dist launcher reached the source: the hub decides Ready.
        Ok(false) => match hub.filter(|h| !h.trim().is_empty()) {
            None => BinaryCheck::Ready,
            Some(h) => match is_older(h, source) {
                Ok(true) => BinaryCheck::HubLagging {
                    hub: h.into(),
                    source: source.into(),
                },
                Ok(false) => BinaryCheck::Ready,
                Err(e) => unknown(e),
            },
        },
    }
}

/// Every side effect of the pipeline. Production: [`LiveOps`]. Tests: a
/// recording fake. The DRIVER ([`drive`]) owns order, refusals and recovery;
/// an implementation owns only "how".
pub(crate) trait UpdateOps {
    // phase 1-2
    fn resolve_root(&mut self) -> Result<PathBuf, UpdateSurfaceError>;
    fn claim(&mut self, root: &Path) -> Result<(), UpdateSurfaceError>;
    // phase 3 (read-only probes, then the remote pin)
    fn git_available(&mut self) -> impl Future<Output = bool> + Send;
    fn python_for_install(
        &mut self,
        root: &Path,
    ) -> impl Future<Output = Result<String, String>> + Send;
    fn tree_refusal(
        &mut self,
        root: &Path,
        kind: UpdateKind,
    ) -> impl Future<Output = Option<UpdateSurfaceError>> + Send;
    fn hub_stop_precheck(&mut self) -> Result<(), String>;
    fn pin_remote(&mut self, root: &Path) -> impl Future<Output = Result<(), String>> + Send;
    // phase 4-5 (first mutations)
    fn arm_gate(&mut self);
    fn stop_hub_and_rename(
        &mut self,
        root: &Path,
        kind: UpdateKind,
    ) -> Result<PrePullRenames, String>;
    // phase 6-7
    fn read_head(&mut self, root: &Path) -> impl Future<Output = Option<String>> + Send;
    fn current_branch(&mut self, root: &Path) -> impl Future<Output = String> + Send;
    fn git_op(
        &mut self,
        root: &Path,
        kind: UpdateKind,
        renames: &PrePullRenames,
        head_before: Option<String>,
    ) -> impl Future<Output = Result<GitOpOutcome, GitOpFailure>> + Send;
    fn head_advance(&mut self, root: &Path) -> impl Future<Output = Result<(), String>> + Send;
    fn abort_restore(&mut self, root: &Path, renames: &PrePullRenames);
    // phase 8-9
    fn run_install_py(
        &mut self,
        root: &Path,
        python_cmd: &str,
    ) -> impl Future<Output = Result<InstallPyRun, String>> + Send;
    fn refresh_after_install(
        &mut self,
        root: &Path,
        branch: &str,
    ) -> impl Future<Output = ()> + Send;
    // phase 10-13
    fn drop_gate(&mut self);
    fn restart_hub(&mut self, root: &Path, ctx: HubRestartContext);
    fn binary_check(
        &mut self,
        root: &Path,
        branch: &str,
    ) -> impl Future<Output = BinaryCheck> + Send;
    fn bookkeeping(&mut self, root: &Path) -> impl Future<Output = ()> + Send;
    fn relaunch(
        &mut self,
        root: &Path,
    ) -> impl Future<Output = Result<RelaunchOutcome, RelaunchError>> + Send;
    // cross-cutting
    fn audit(&mut self, operation: &str, detail: Value);
    fn progress(&mut self, stage: &str, message: &str, percentage: f32);
}

// ---------------------------------------------------------------------------
// The driver
// ---------------------------------------------------------------------------

/// Pipeline state the driver threads between phases.
struct DriveState {
    root: Option<PathBuf>,
    started_ms: i64,
}

/// Run the thirteen phases with `ops`, recording each in `ledger`. Every
/// failure leg is written ONCE here as `update_orchestrator_complete
/// {success:false}`.
pub(crate) async fn drive<O: UpdateOps + Send>(
    ops: &mut O,
    kind: UpdateKind,
    ledger: &mut PhaseLedger,
) -> Result<UpdateOutcome, UpdateSurfaceError> {
    let mut st = DriveState {
        root: None,
        started_ms: chrono::Utc::now().timestamp_millis(),
    };
    let result = drive_phases(ops, kind, ledger, &mut st).await;
    if let Err(e) = &result {
        ops.audit(
            "update_orchestrator_complete",
            json!({
                "success": false,
                "surface": SURFACE,
                "kind": kind,
                "failed_phase": ledger.failed_phase(),
                "error_kind": e.kind(),
                "message": e.message(),
                "duration_ms": chrono::Utc::now().timestamp_millis() - st.started_ms,
                "install_path": st.root.as_ref().map(|p| p.display().to_string()),
                "phases": ledger.entries,
            }),
        );
        tracing::warn!(
            "[vct] {}: {:?} update failed at {:?}: {}",
            SURFACE,
            kind,
            ledger.failed_phase(),
            e.message()
        );
    }
    result
}

async fn drive_phases<O: UpdateOps + Send>(
    ops: &mut O,
    kind: UpdateKind,
    ledger: &mut PhaseLedger,
    st: &mut DriveState,
) -> Result<UpdateOutcome, UpdateSurfaceError> {
    // ── 1. resolve the install root, identity-checked ─────────────────────
    let root = match ops.resolve_root() {
        Ok(r) => r,
        Err(e) => {
            ledger.fail(Phase::ResolveRoot, e.message());
            return Err(e);
        }
    };
    st.root = Some(root.clone());
    ledger.done_with(Phase::ResolveRoot, root.display().to_string());
    let log_path = update_failure::install_log_path(&root);

    // ── 2. claim (in-process + cross-process) ─────────────────────────────
    if let Err(e) = ops.claim(&root) {
        ledger.fail(Phase::Claim, e.message());
        return Err(e);
    }
    ledger.done(Phase::Claim);

    // ── 3. preflight: every refusal, before ANY mutation ──────────────────
    let python_cmd = match preflight(ops, &root, kind).await {
        Ok(p) => p,
        Err(e) => {
            ledger.fail(Phase::Preflight, e.message());
            return Err(e);
        }
    };
    ledger.done(Phase::Preflight);
    let head_before = ops.read_head(&root).await;
    ops.audit(
        "update_orchestrator_start",
        json!({
            "surface": SURFACE,
            "kind": kind,
            "old_version": RUNNING_VERSION,
            "source_commit": head_before,
            "install_path": root.display().to_string(),
        }),
    );

    // ── 4. MCP sweep + update gate (after every refusal — L2-F14) ─────────
    ops.arm_gate();
    ledger.done(Phase::SweepAndGate);

    // ── 5. hub stop + binary renames ──────────────────────────────────────
    let renames = match ops.stop_hub_and_rename(&root, kind) {
        Ok(r) => r,
        Err(reason) => {
            let e = UpdateSurfaceError::Refused {
                code: "hub_stop_failed",
                reason,
            };
            ledger.fail(Phase::HubStopAndRenames, e.message());
            return Err(e);
        }
    };
    ledger.done(Phase::HubStopAndRenames);

    // ── 6. the git operation, by kind ─────────────────────────────────────
    let git = if kind == UpdateKind::ApplyOnly {
        let branch = ops.current_branch(&root).await;
        ledger.skip(Phase::GitOp, "ApplyOnly runs no git operation");
        GitOpOutcome {
            already_up_to_date: false,
            dist_binary_stale: false,
            branch,
            reset_backup: None,
        }
    } else {
        match ops.git_op(&root, kind, &renames, head_before.clone()).await {
            Ok(g) => {
                ledger.done(Phase::GitOp);
                g
            }
            Err(f) => {
                if !f.restored {
                    ops.abort_restore(&root, &renames);
                }
                ledger.fail(Phase::GitOp, f.error.message());
                return Err(f.error);
            }
        }
    };

    if git.already_up_to_date {
        // The pull sequence restored the binaries and restarted the hub
        // itself (its "already up to date" heal). Nothing was applied.
        for phase in [
            Phase::HeadAdvance,
            Phase::InstallPy,
            Phase::DbReopenAndRefresh,
        ] {
            ledger.skip(phase, "already up to date");
        }
        ops.drop_gate();
        ledger.done_with(
            Phase::GateDropAndHubRestart,
            "gate dropped; hub restarted by the pull sequence",
        );
        ledger.skip(Phase::BinaryRefresh, "already up to date");
        ops.audit(
            "update_orchestrator_complete",
            json!({
                "success": true,
                "surface": SURFACE,
                "kind": kind,
                "note": "already_up_to_date",
                "duration_ms": chrono::Utc::now().timestamp_millis() - st.started_ms,
                "branch": git.branch,
            }),
        );
        ledger.skip(Phase::Bookkeeping, "nothing changed");
        let (restarted, note) = if git.dist_binary_stale {
            relaunch_phase(ops, &root, ledger).await
        } else {
            ledger.skip(Phase::Relaunch, "the running launcher is current");
            (false, None)
        };
        let message = compose_message("Already up to date.", None, note.as_deref());
        if !restarted {
            ops.progress("done", &message, 100.0);
        }
        return Ok(UpdateOutcome {
            kind,
            head_after: head_before.clone(),
            head_before,
            install_py_ran: false,
            restarted,
            log_path,
            message,
            phases: ledger.entries.clone(),
            reset_backup: None,
        });
    }

    // ── 7. HEAD reached upstream ──────────────────────────────────────────
    match kind {
        UpdateKind::ApplyOnly => ledger.skip(Phase::HeadAdvance, "no git operation ran"),
        UpdateKind::PullFf => ledger.done_with(Phase::HeadAdvance, "verified by the pull sequence"),
        _ => match ops.head_advance(&root).await {
            Ok(()) => ledger.done(Phase::HeadAdvance),
            Err(msg) => {
                ops.abort_restore(&root, &renames);
                let e = UpdateSurfaceError::Raw(msg);
                ledger.fail(Phase::HeadAdvance, e.message());
                return Err(e);
            }
        },
    }

    // ── 8. install.py --update (DB closed, streamed) ──────────────────────
    let run = match ops.run_install_py(&root, &python_cmd).await {
        Ok(run) => run,
        Err(spawn_error) => {
            ops.abort_restore(&root, &renames);
            let e = update_failure::install_spawn_failed(&spawn_error, &root);
            ledger.fail(Phase::InstallPy, e.message());
            return Err(e);
        }
    };
    if !run.success {
        ops.abort_restore(&root, &renames);
        let e = update_failure::install_failed(&run, &root);
        ledger.fail(Phase::InstallPy, e.message());
        return Err(e);
    }
    ledger.done(Phase::InstallPy);

    // ── 9. DB reopened (inside phase 8's guard) + cached state refreshed ──
    ops.refresh_after_install(&root, &git.branch).await;
    ledger.done(Phase::DbReopenAndRefresh);

    // ── 10. gate dropped BEFORE the binary check; hub restarted ───────────
    ops.drop_gate();
    if cfg!(windows) {
        ledger.done_with(
            Phase::GateDropAndHubRestart,
            "gate dropped; hub restart follows the stage-1 handoff decision (C-1)",
        );
    } else {
        ops.restart_hub(&root, HubRestartContext::PostInstall);
        ledger.done(Phase::GateDropAndHubRestart);
    }

    // ── 11. the dist binary vs source: one read, no pull ──────────────────
    let check = ops.binary_check(&root, &git.branch).await;
    let binary_note = match &check {
        BinaryCheck::RunningCurrent => {
            ledger.done_with(Phase::BinaryRefresh, "running launcher already at source");
            None
        }
        BinaryCheck::Ready => {
            ledger.done(Phase::BinaryRefresh);
            None
        }
        BinaryCheck::Partial { dist, source, .. } => {
            let n = format!(
                "the launcher binary on disk (v{}) is below the source (v{}) but newer than \
                 the running one; relaunching into it",
                dist, source
            );
            ledger.done_with(Phase::BinaryRefresh, n.clone());
            Some(n)
        }
        BinaryCheck::NotPublished {
            running,
            dist,
            source,
        } => {
            let n = format!(
                "the launcher binary for v{} is not published yet (on disk: v{}); the launcher \
                 keeps running v{} and will offer the restart when it lands",
                source, dist, running
            );
            ledger.done_with(Phase::BinaryRefresh, n.clone());
            Some(n)
        }
        BinaryCheck::HubLagging { hub, source } => {
            let n = format!(
                "the vct-hub binary on disk (v{}) is below the source (v{}); the hub keeps \
                 running that binary until its release build lands",
                hub, source
            );
            ledger.done_with(Phase::BinaryRefresh, n.clone());
            Some(n)
        }
        BinaryCheck::Unknown { reason } => {
            ledger.done_with(
                Phase::BinaryRefresh,
                format!("versions not ranked: {}", reason),
            );
            None
        }
    };

    // ── 12. bookkeeping + the success row, BEFORE the restart hop ─────────
    ops.bookkeeping(&root).await;
    let head_after = ops.read_head(&root).await;
    ops.audit(
        "update_orchestrator_complete",
        json!({
            "success": true,
            "surface": SURFACE,
            "kind": kind,
            "duration_ms": chrono::Utc::now().timestamp_millis() - st.started_ms,
            "old_version": RUNNING_VERSION,
            "new_sha": head_after,
            "branch": git.branch,
        }),
    );
    ledger.done(Phase::Bookkeeping);

    // ── 13. relaunch into the dist binary (version-guarded) ───────────────
    let (restarted, relaunch_note) = relaunch_phase(ops, &root, ledger).await;

    let head_line = if restarted {
        "Orchestrator updated; relaunching into the new launcher binary."
    } else {
        "Orchestrator updated."
    };
    let head_line = match &git.reset_backup {
        Some(b) => format!("{} {}", head_line, b.describe()),
        None => head_line.to_string(),
    };
    let message = compose_message(&head_line, binary_note.as_deref(), relaunch_note.as_deref());
    // The modal's completion signal when this process keeps running (a
    // relaunch exits it, and the new launcher is the signal).
    if !restarted {
        ops.progress("done", &message, 100.0);
    }
    Ok(UpdateOutcome {
        kind,
        head_before,
        head_after,
        install_py_ran: true,
        restarted,
        log_path,
        message,
        phases: ledger.entries.clone(),
        reset_backup: git.reset_backup,
    })
}

/// Phase 3. Order: read-only probes first, the remote pin (a git-config
/// write) last — so every refusal that CAN be decided without writing
/// anything is decided before anything is written.
async fn preflight<O: UpdateOps + Send>(
    ops: &mut O,
    root: &Path,
    kind: UpdateKind,
) -> Result<String, UpdateSurfaceError> {
    if !ops.git_available().await {
        return Err(UpdateSurfaceError::Refused {
            code: "git_missing",
            reason: "Update refused before anything was changed: git was not found on PATH.".into(),
        });
    }
    // A missing interpreter is a REFUSAL here, never a post-pull
    // source-only degrade (L2-F04 / D2).
    let python_cmd =
        ops.python_for_install(root)
            .await
            .map_err(|reason| UpdateSurfaceError::Refused {
                code: "python_missing",
                reason,
            })?;
    if let Some(refusal) = ops.tree_refusal(root, kind).await {
        return Err(refusal);
    }
    ops.hub_stop_precheck()
        .map_err(|reason| UpdateSurfaceError::Refused {
            code: "hub_state_unreadable",
            reason,
        })?;
    ops.pin_remote(root)
        .await
        .map_err(|reason| UpdateSurfaceError::Refused {
            code: "remote_pin_failed",
            reason: format!(
                "Update refused before anything was changed: could not pin the upstream \
                 remote: {}",
                reason
            ),
        })?;
    Ok(python_cmd)
}

/// Phase 13. A refusal is not an update failure — the update is applied; the
/// launcher simply is not restarted (the note says why). On Windows the hub
/// restart follows the handoff decision, so a relaunch that did not happen
/// still owes it.
async fn relaunch_phase<O: UpdateOps + Send>(
    ops: &mut O,
    root: &Path,
    ledger: &mut PhaseLedger,
) -> (bool, Option<String>) {
    match ops.relaunch(root).await {
        Ok(RelaunchOutcome::HandoffExit) => {
            ledger.done_with(Phase::Relaunch, "stage-1 handoff: vct-updater relaunches");
            (true, None)
        }
        Ok(RelaunchOutcome::Spawned { exe }) => {
            ledger.done_with(Phase::Relaunch, exe.display().to_string());
            (true, None)
        }
        Err(e) => {
            if cfg!(windows) {
                ops.restart_hub(root, HubRestartContext::PostInstall);
            }
            let note = match &e {
                RelaunchError::Refused(RelaunchRefusal::NotNewer { .. }) => {
                    format!("The launcher was not restarted: {}.", e)
                }
                _ => format!(
                    "The launcher was not restarted ({}); quit and relaunch it to load the new \
                     binary.",
                    e
                ),
            };
            ledger.skip(Phase::Relaunch, e.to_string());
            (false, Some(note))
        }
    }
}

fn compose_message(head: &str, binary: Option<&str>, relaunch: Option<&str>) -> String {
    let mut m = head.to_string();
    if let Some(b) = binary {
        m.push(' ');
        m.push_str(&capitalise(b));
        if !b.ends_with('.') {
            m.push('.');
        }
    }
    if let Some(r) = relaunch {
        m.push(' ');
        m.push_str(r);
    }
    m
}

fn capitalise(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

/// Refuse a hard reset of anything that is not provably an orchestrator
/// clone: structural markers AND `vct-module.json` id `orchestrator`
/// (`install_root::is_orchestrator_clone`). Checked in preflight and again
/// immediately before the reset.
pub(crate) fn reset_hard_identity_refusal(root: &Path) -> Option<UpdateSurfaceError> {
    use vct_launcher_core::services::install_root;
    if install_root::looks_like_orchestrator_root(root) && install_root::is_orchestrator_clone(root)
    {
        return None;
    }
    Some(UpdateSurfaceError::Refused {
        code: "reset_target_not_orchestrator",
        reason: format!(
            "Refusing `git reset --hard` in {}: it is not an orchestrator clone \
             (vct-module.json with id \"{}\" is required). Nothing was changed.",
            root.display(),
            install_root::ORCHESTRATOR_MODULE_ID
        ),
    })
}

// ---------------------------------------------------------------------------
// Production side effects
// ---------------------------------------------------------------------------

/// The live implementation of [`UpdateOps`]. Holds the claims and the gate
/// for the life of the run; their `Drop`s release them on every exit path.
pub(crate) struct LiveOps<'w, R: Runtime> {
    app: AppHandle<R>,
    window: Option<&'w Window>,
    flight: Option<crate::commands::single_flight::SingleFlightGuard>,
    machine_lock: Option<crate::commands::single_flight::UpdateLockGuard>,
    gate: Option<crate::commands::update_gate::UpdateInProgressGuard>,
    started_ms: i64,
}

impl<'w, R: Runtime> LiveOps<'w, R> {
    /// `flight`: a single-flight claim the CALLER already holds (a conflict
    /// resolver that did destructive work under it) — handed over so ONE
    /// claim spans that work and the update it unblocks.
    fn new(
        app: AppHandle<R>,
        window: Option<&'w Window>,
        flight: Option<crate::commands::single_flight::SingleFlightGuard>,
    ) -> Self {
        Self {
            app,
            window,
            flight,
            machine_lock: None,
            gate: None,
            started_ms: chrono::Utc::now().timestamp_millis(),
        }
    }

    fn emit(&self, stage: &str, message: &str, percentage: f32) {
        if let Some(w) = self.window {
            crate::commands::installer::emit_progress(w, stage, message, percentage);
        }
    }
}

impl<'w, R: Runtime> UpdateOps for LiveOps<'w, R> {
    fn resolve_root(&mut self) -> Result<PathBuf, UpdateSurfaceError> {
        use vct_launcher_core::services::install_root;
        let refused =
            |code: &'static str, reason: String| UpdateSurfaceError::Refused { code, reason };
        let db = self.app.try_state::<crate::db::Db>().ok_or_else(|| {
            refused(
                "no_database",
                "The launcher database is not available, so the install root cannot be \
                 resolved. Restart the launcher and try again."
                    .into(),
            )
        })?;
        let resolved = crate::commands::installer::resolve_root_with_db(&db)
            .map_err(|e| refused("root_unresolved", e.to_string()))?;
        resolved
            .require_exe_inside()
            .map_err(|e| refused("exe_outside_clone", e.to_string()))?;
        if !install_root::is_orchestrator_clone(&resolved.path) {
            return Err(refused(
                "not_an_orchestrator_clone",
                format!(
                    "{} is not an orchestrator clone (vct-module.json with id \"{}\") — \
                     refusing to update it.",
                    resolved.path.display(),
                    install_root::ORCHESTRATOR_MODULE_ID
                ),
            ));
        }
        Ok(resolved.path)
    }

    fn claim(&mut self, _root: &Path) -> Result<(), UpdateSurfaceError> {
        if self.flight.is_none() {
            let flight = crate::commands::single_flight::begin_orchestrator_update_or_refuse()
                .map_err(|reason| UpdateSurfaceError::Refused {
                    code: "already_running",
                    reason,
                })?;
            self.flight = Some(flight);
        }
        let lock = crate::commands::single_flight::acquire_update_lock().map_err(|reason| {
            UpdateSurfaceError::Refused {
                code: "already_running",
                reason,
            }
        })?;
        self.machine_lock = Some(lock);
        Ok(())
    }

    fn git_available(&mut self) -> impl Future<Output = bool> + Send {
        crate::commands::installer::check_command_exists("git")
    }

    fn python_for_install(
        &mut self,
        root: &Path,
    ) -> impl Future<Output = Result<String, String>> + Send {
        let install_py = root.join("install.py");
        async move {
            let (has_python, _version, cmd) = crate::commands::installer::detect_python().await;
            if !has_python || cmd.trim().is_empty() {
                return Err(
                    "Update refused before anything was changed: no Python interpreter was \
                     found, so install.py --update could not run after the pull. Install \
                     Python 3.10+ (or fix PATH) and try again."
                        .to_string(),
                );
            }
            if !install_py.is_file() {
                return Err(format!(
                    "Update refused before anything was changed: {} is missing, so the \
                     update could not be applied after the pull.",
                    install_py.display()
                ));
            }
            Ok(cmd)
        }
    }

    fn tree_refusal(
        &mut self,
        root: &Path,
        kind: UpdateKind,
    ) -> impl Future<Output = Option<UpdateSurfaceError>> + Send {
        let root = root.to_path_buf();
        let app = self.app.clone();
        async move {
            if !root.join(".git").exists() {
                return Some(UpdateSurfaceError::Refused {
                    code: "not_a_git_repo",
                    reason: format!(
                        "{} is not a git repository — it cannot be updated in place.",
                        root.display()
                    ),
                });
            }
            if crate::commands::installer::update_requires_hard_cut(&root) {
                tracing::info!(
                    "[vct] {}: installed version is below min_upgradable_from — a guided \
                     hard-cut would be required (not wired; logged only)",
                    SURFACE
                );
            }
            if kind == UpdateKind::ResetHard {
                // The reset is the rescue for a wedged tree, so an in-progress
                // merge is not a refusal here — identity is.
                return reset_hard_identity_refusal(&root);
            }
            match crate::commands::update_pipeline::run_preflight_refusals(&root, SURFACE).await
            {
                Ok(()) if kind == UpdateKind::Resume => resume_refusal(&root).await,
                Ok(()) => None,
                Err(err) => {
                    if let Some(db) = app.try_state::<crate::db::Db>() {
                        let _ = db.audit(
                            "update_orchestrator_refused_merge_in_progress",
                            None,
                            None,
                            &json!({"install_path": root.display().to_string(), "surface": SURFACE}),
                        );
                    }
                    Some(update_failure::from_pipeline_error(err).0)
                }
            }
        }
    }

    fn hub_stop_precheck(&mut self) -> Result<(), String> {
        crate::commands::update_pipeline::hub_stop_precheck()
    }

    fn pin_remote(&mut self, root: &Path) -> impl Future<Output = Result<(), String>> + Send {
        let root = root.to_path_buf();
        async move { crate::commands::self_update::ensure_upstream_remote(&root).await }
    }

    fn arm_gate(&mut self) {
        self.gate = Some(crate::commands::update_pipeline::arm_update_gate(SURFACE));
    }

    fn stop_hub_and_rename(
        &mut self,
        root: &Path,
        kind: UpdateKind,
    ) -> Result<PrePullRenames, String> {
        let (operation, before) = match kind {
            UpdateKind::ResetHard => ("update", Some("git reset --hard")),
            UpdateKind::ApplyOnly => ("update", Some("install.py --update")),
            UpdateKind::Merge => ("merge", Some("git pull")),
            UpdateKind::Rebase => ("rebase", Some("git rebase")),
            UpdateKind::Resume => ("resume", None),
            UpdateKind::PullFf => ("update", Some("git pull")),
        };
        let window = self.window;
        crate::commands::update_pipeline::stop_hub_and_rename_binaries_aside(
            root,
            operation,
            before,
            |stage: &str, message: &str, pct: f32| {
                if let Some(w) = window {
                    crate::commands::installer::emit_progress(w, stage, message, pct);
                }
            },
        )
    }

    fn read_head(&mut self, root: &Path) -> impl Future<Output = Option<String>> + Send {
        let root = root.to_path_buf();
        async move { crate::commands::installer::read_head_sha(&root).await }
    }

    fn current_branch(&mut self, root: &Path) -> impl Future<Output = String> + Send {
        let root = root.to_path_buf();
        async move {
            crate::commands::git_cmd::resolve_branch(&root)
                .await
                .map(|s| s.name)
                .unwrap_or_else(|_| crate::commands::git_cmd::FALLBACK_BRANCH.to_string())
        }
    }

    fn git_op(
        &mut self,
        root: &Path,
        kind: UpdateKind,
        renames: &PrePullRenames,
        head_before: Option<String>,
    ) -> impl Future<Output = Result<GitOpOutcome, GitOpFailure>> + Send {
        let root = root.to_path_buf();
        let renames = renames.clone();
        let window = self.window;
        let app = self.app.clone();
        let started_ms = self.started_ms;
        async move {
            match kind {
                UpdateKind::PullFf => {
                    pull_ff_git_op(&app, window, &root, &renames, head_before, started_ms).await
                }
                UpdateKind::ResetHard => {
                    let backups = vct_launcher_core::paths::vct_root_dir().join("backups");
                    let progress = |message: &str| {
                        if let Some(w) = window {
                            crate::commands::installer::emit_progress(w, "update", message, 10.0);
                        }
                    };
                    reset_hard_git_op(&root, &backups, &progress).await
                }
                UpdateKind::Merge | UpdateKind::Rebase | UpdateKind::Resume => {
                    let op = recovery_git_op(&root, kind, window).await;
                    finish_recovery_git_op(&app, &root, &renames, op).await
                }
                // The driver never asks ApplyOnly for a git operation.
                UpdateKind::ApplyOnly => Err(GitOpFailure {
                    error: UpdateSurfaceError::Raw(
                        "internal: ApplyOnly reached the git operation phase".into(),
                    ),
                    restored: false,
                }),
            }
        }
    }

    fn head_advance(&mut self, root: &Path) -> impl Future<Output = Result<(), String>> + Send {
        let root = root.to_path_buf();
        async move {
            use crate::commands::installer::HeadAdvanceOutcome;
            match crate::commands::installer::assert_head_reached_upstream(&root).await? {
                HeadAdvanceOutcome::Reached => {}
                // Not blocking, but recorded (v0.2.92 WP-13 item 8).
                HeadAdvanceOutcome::Unverified { error } => {
                    let branch = crate::commands::installer::resolve_pull_branch(&root).await;
                    crate::commands::git_user_editable_merge::write_launcher_update_post_pull_unverified_deferral(
                        &root, &branch, &error,
                    );
                }
            }
            Ok(())
        }
    }

    fn abort_restore(&mut self, root: &Path, renames: &PrePullRenames) {
        crate::commands::installer::abort_update_restore_binaries_and_hub(
            root,
            renames.launcher.as_deref(),
            renames.hub.as_deref(),
        );
    }

    fn run_install_py(
        &mut self,
        root: &Path,
        python_cmd: &str,
    ) -> impl Future<Output = Result<InstallPyRun, String>> + Send {
        self.emit("install", "Applying updates...", 40.0);
        if let Some(g) = self.gate.as_mut() {
            g.advance_phase(crate::commands::update_gate::Phase::InstallPy);
        }
        let root = root.to_path_buf();
        let python_cmd = python_cmd.to_string();
        let app = self.app.clone();
        let window = self.window;
        async move {
            // v0.2.60: close launcher.db for the install.py window (Windows
            // writer lock); the guard reopens on every path, and force-quits
            // if the reopen fails.
            let mut db_guard = crate::commands::installer::DbUpdateClosedGuard::new(app);
            let run = crate::commands::update_pipeline::run_install_py_update(
                &root,
                &python_cmd,
                SURFACE,
                window,
            )
            .await;
            db_guard.reopen();
            run
        }
    }

    fn refresh_after_install(
        &mut self,
        root: &Path,
        branch: &str,
    ) -> impl Future<Output = ()> + Send {
        if let Some(g) = self.gate.as_mut() {
            g.advance_phase(crate::commands::update_gate::Phase::BinaryRefresh);
        }
        let root = root.to_path_buf();
        let branch = branch.to_string();
        async move {
            crate::commands::self_update::refresh_cached_state_after_pull(&root, &branch).await;
        }
    }

    fn drop_gate(&mut self) {
        if let Some(mut g) = self.gate.take() {
            g.disarm_and_cleanup();
        }
    }

    fn restart_hub(&mut self, root: &Path, ctx: HubRestartContext) {
        self.emit("update", "Starting vct-hub...", 97.0);
        if let Err(e) = crate::commands::installer::ensure_hub_started_after_update(root, ctx) {
            tracing::warn!(
                "[vct] {}: vct-hub restart reported {} (non-fatal; the next launcher boot \
                 retries)",
                SURFACE,
                e
            );
        }
    }

    fn binary_check(
        &mut self,
        root: &Path,
        branch: &str,
    ) -> impl Future<Output = BinaryCheck> + Send {
        self.emit("update", "Checking the new launcher binary...", 96.0);
        let root = root.to_path_buf();
        let branch = branch.to_string();
        async move {
            let source = crate::commands::installer::read_source_version(&root);
            let dist = crate::commands::installer::read_on_disk_binary_version(&root);
            let hub = crate::commands::installer::read_on_disk_hub_version(&root);
            let check = decide_binary_refresh(
                RUNNING_VERSION,
                source.as_deref(),
                dist.as_deref(),
                hub.as_deref(),
            );
            use crate::commands::git_user_editable_merge::{
                write_launcher_update_diverged_deferral, LauncherUpdateDivergedKind,
            };
            match &check {
                BinaryCheck::Partial {
                    running,
                    dist,
                    source,
                } => {
                    write_launcher_update_diverged_deferral(
                        &root,
                        &branch,
                        LauncherUpdateDivergedKind::PartialBinaryRefresh {
                            running: running.clone(),
                            on_disk: dist.clone(),
                            detail: format!("dist v{} is below source v{}", dist, source),
                        },
                    );
                }
                BinaryCheck::NotPublished {
                    running,
                    dist,
                    source,
                } => {
                    write_launcher_update_diverged_deferral(
                        &root,
                        &branch,
                        LauncherUpdateDivergedKind::BinaryRefreshTimeout {
                            running: running.clone(),
                            on_disk: dist.clone(),
                            detail: format!(
                                "no launcher binary for v{} is on disk yet (the release may \
                                 still be committing it)",
                                source
                            ),
                        },
                    );
                }
                _ => {}
            }
            check
        }
    }

    fn bookkeeping(&mut self, root: &Path) -> impl Future<Output = ()> + Send {
        let root = root.to_path_buf();
        let app = self.app.clone();
        async move {
            let exe = crate::commands::restart::dist_launcher_path(&root);
            if exe.is_file() {
                if let Err(e) =
                    crate::commands::desktop_shortcut::refresh_desktop_shortcut(&root, &exe)
                {
                    tracing::warn!("[vct] {}: desktop shortcut refresh failed: {}", SURFACE, e);
                }
            }
            let Some(db) = app.try_state::<crate::db::Db>() else {
                tracing::warn!("[vct] {}: no Db state — bookkeeping skipped", SURFACE);
                return;
            };
            crate::commands::installer::mark_hardware_redetect_pending_after_update(db.inner());
            // v0.2.44 V44-G4 Trigger B, now BEFORE the restart hop (L2-F12:
            // it used to run after `restart_launcher` had requested exit).
            let detail =
                if crate::commands::module_service::auto_retry_on_orchestrator_update_enabled(&db) {
                    let reports = crate::commands::module_service::retry_failed_module_installs(
                        None, &db, None,
                    )
                    .await;
                    let mut counts = std::collections::BTreeMap::<String, u32>::new();
                    for r in &reports {
                        *counts.entry(r.decision.clone()).or_insert(0) += 1;
                    }
                    json!({"trigger": SURFACE, "total": reports.len(), "summary": counts})
                } else {
                    json!({"trigger": SURFACE, "skipped": "disabled_by_setting"})
                };
            let _ = db.audit("module_install_auto_retry_sweep", None, None, &detail);
        }
    }

    fn relaunch(
        &mut self,
        root: &Path,
    ) -> impl Future<Output = Result<RelaunchOutcome, RelaunchError>> + Send {
        self.emit("restart", "Update applied — restarting launcher...", 98.0);
        // W2R-06: a relaunch ends in `app.exit(0)`, which never drops `self`
        // — release both claims NOW (the update is applied and audited), so
        // no `update.lock` naming a dead pid is left behind.
        self.machine_lock = None;
        self.flight = None;
        let root = root.to_path_buf();
        let app = self.app.clone();
        async move {
            let hub_root = root.clone();
            crate::commands::restart::relaunch(
                &app,
                crate::commands::restart::RelaunchTarget::Dist,
                &root,
                RUNNING_VERSION,
                move || {
                    // C-1: on Windows the hub restart follows the handoff
                    // decision; POSIX already restarted it in phase 10.
                    if cfg!(windows) {
                        let _ = crate::commands::installer::ensure_hub_started_after_update(
                            &hub_root,
                            HubRestartContext::PostInstall,
                        );
                    }
                },
            )
            .await
        }
    }

    fn audit(&mut self, operation: &str, detail: Value) {
        if let Some(db) = self.app.try_state::<crate::db::Db>() {
            let _ = db.audit(operation, None, None, &detail);
        }
    }

    fn progress(&mut self, stage: &str, message: &str, percentage: f32) {
        self.emit(stage, message, percentage);
    }
}

/// `PullFf`'s git operation: the shared pull sequence
/// (`update_pipeline::pull_to_upstream`) — fetch, A0, F1, reconcile, pull,
/// classification, HEAD-advance backstop — after the prior resume state is
/// cleared. Every failure leg of that sequence restores the binaries and the
/// hub itself, hence `restored: true`.
async fn pull_ff_git_op<R: Runtime>(
    app: &AppHandle<R>,
    window: Option<&Window>,
    root: &Path,
    renames: &PrePullRenames,
    head_before: Option<String>,
    started_ms: i64,
) -> Result<GitOpOutcome, GitOpFailure> {
    crate::commands::update_pipeline::clear_prior_resume_state(root);
    let label = root.display().to_string();
    let start_branch = crate::commands::git_cmd::resolve_branch(root)
        .await
        .map(|s| s.name)
        .unwrap_or_else(|_| crate::commands::git_cmd::FALLBACK_BRANCH.to_string());
    let opts = crate::commands::update_pipeline::UpdatePipelineOptions {
        surface: SURFACE,
        emit_progress_to: window,
        install_path_label: &label,
        start_branch: &start_branch,
        head_sha_before: head_before,
        update_start_ms: started_ms,
    };
    let audit = |rows: AuditRows| {
        if let Some(db) = app.try_state::<crate::db::Db>() {
            for (op, detail) in rows {
                let _ = db.audit(&op, None, None, &detail);
            }
        }
    };
    match crate::commands::update_pipeline::pull_to_upstream(root, &opts, renames).await {
        Ok(pulled) => {
            audit(pulled.db_audit);
            Ok(GitOpOutcome {
                already_up_to_date: pulled.already_up_to_date,
                dist_binary_stale: pulled.dist_binary_stale,
                branch: pulled.pull_branch,
                reset_backup: None,
            })
        }
        Err(err) => {
            let (error, rows) = update_failure::from_pipeline_error(err);
            audit(rows);
            Err(GitOpFailure {
                error,
                restored: true,
            })
        }
    }
}

/// `Merge` / `Rebase` / `Resume`'s git operation (phase 6), as the pipeline
/// condition: `update_pipeline` owns the git work; nothing here restores.
async fn recovery_git_op(
    root: &Path,
    kind: UpdateKind,
    window: Option<&Window>,
) -> Result<crate::commands::update_pipeline::RecoveryGitOp, RecoveryFailure> {
    use crate::commands::update_pipeline::{self as pipeline, RecoveryGitOp, ResumeVerdict};
    match kind {
        UpdateKind::Merge => pipeline::merge_upstream(root, SURFACE, window)
            .await
            .map_err(RecoveryFailure::Pipeline),
        UpdateKind::Rebase => pipeline::rebase_onto_upstream(root, SURFACE, window)
            .await
            .map_err(RecoveryFailure::Pipeline),
        _ => match pipeline::classify_resume(root).await {
            // The resume consumes its record either way.
            ResumeVerdict::Proceed { branch } => {
                pipeline::clear_prior_resume_state(root);
                Ok(RecoveryGitOp {
                    already_up_to_date: false,
                    branch,
                })
            }
            ResumeVerdict::NothingPending { branch } => {
                pipeline::clear_prior_resume_state(root);
                Ok(RecoveryGitOp {
                    already_up_to_date: true,
                    branch,
                })
            }
            // Preflight refused these; the tree moved in between.
            ResumeVerdict::Refuse { code, message, .. } => {
                Err(RecoveryFailure::Surface(UpdateSurfaceError::Refused {
                    code,
                    reason: message,
                }))
            }
        },
    }
}

/// Why a recovery git operation stopped.
enum RecoveryFailure {
    Pipeline(crate::commands::update_pipeline::UpdatePipelineError),
    Surface(UpdateSurfaceError),
}

/// Map a recovery git operation onto the driver's [`GitOpOutcome`]. The
/// driver restores on failure (`restored: false`); "already up to date"
/// restores HERE, because the driver's up-to-date leg expects the git
/// operation to have put the binaries and the hub back (as the pull sequence
/// does) — then reconciles the dist binary at rest like the pull does.
async fn finish_recovery_git_op<R: Runtime>(
    app: &AppHandle<R>,
    root: &Path,
    renames: &PrePullRenames,
    op: Result<crate::commands::update_pipeline::RecoveryGitOp, RecoveryFailure>,
) -> Result<GitOpOutcome, GitOpFailure> {
    match op {
        Ok(done) if done.already_up_to_date => {
            crate::commands::installer::abort_update_restore_binaries_and_hub(
                root,
                renames.launcher.as_deref(),
                renames.hub.as_deref(),
            );
            let heal = crate::services::binary_freshness::reconcile_dist_at_rest(root).await;
            Ok(GitOpOutcome {
                already_up_to_date: true,
                dist_binary_stale: heal.is_stale(),
                branch: done.branch,
                reset_backup: None,
            })
        }
        Ok(done) => Ok(GitOpOutcome {
            already_up_to_date: false,
            dist_binary_stale: false,
            branch: done.branch,
            reset_backup: None,
        }),
        Err(RecoveryFailure::Surface(error)) => Err(GitOpFailure {
            error,
            restored: false,
        }),
        Err(RecoveryFailure::Pipeline(err)) => {
            let (error, rows) = update_failure::from_pipeline_error(err);
            if let Some(db) = app.try_state::<crate::db::Db>() {
                for (op, detail) in rows {
                    let _ = db.audit(&op, None, None, &detail);
                }
            }
            Err(GitOpFailure {
                error,
                restored: false,
            })
        }
    }
}

/// Phase 3 for `Resume`: every refusal [`update_pipeline::classify_resume`]
/// can decide from the sentinel and git state, before any mutation. A
/// provably stale sentinel (HEAD never moved past the conflict) is cleared
/// with the refusal so the badge stops offering a resume that cannot happen.
///
/// [`update_pipeline::classify_resume`]: crate::commands::update_pipeline::classify_resume
async fn resume_refusal(root: &Path) -> Option<UpdateSurfaceError> {
    use crate::commands::update_pipeline::{classify_resume, clear_prior_resume_state, ResumeVerdict};
    match classify_resume(root).await {
        ResumeVerdict::Refuse {
            code,
            message,
            stale_sentinel,
        } => {
            if stale_sentinel {
                clear_prior_resume_state(root);
            }
            Some(UpdateSurfaceError::Refused {
                code,
                reason: message,
            })
        }
        ResumeVerdict::Proceed { .. } | ResumeVerdict::NothingPending { .. } => None,
    }
}

/// Run one git command in `root` for the reset backup (through the one git
/// runner, `git_cmd::run_git_raw_env`); stdout trimmed, or the failure worded
/// with the command.
async fn backup_git(root: &Path, args: &[&str], env: &[(&str, &str)]) -> Result<String, String> {
    let out = crate::commands::git_cmd::run_git_raw_env(root, args, env).await?;
    if !out.status.success() {
        return Err(format!(
            "`git {}` failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// The canonical path of a Windows phase-5 rename artefact, or `None` when
/// `rel` is not one (review W3R-03). Pure.
///
/// The launcher itself writes exactly one shape before the git operation
/// (`binary_freshness::pre_pull_rename_running_binary`, the hub rename in
/// `installer.rs`): `launcher/dist/<platform>/<binary>.old-<pid>` next to the
/// tracked `<binary>` it was renamed from. So an artefact is: directly inside
/// `launcher/dist/<platform>/`, named `<stem>.old-<digits>` (the stem parsed
/// by `binary_freshness::canonical_path_for_backup`, the PID digits-only as
/// the boot sweep requires), AND `launcher/dist/<platform>/<stem>` tracked at
/// HEAD. Anything else — `vct-launcher.old-may7`, a `.old-<pid>` elsewhere,
/// one whose canonical file is not tracked — is not ours and is snapshotted
/// like any other untracked file.
pub(crate) fn launcher_rename_artefact_canonical(
    rel: &str,
    tracked_at_head: &std::collections::HashSet<String>,
) -> Option<String> {
    let parts: Vec<&str> = rel.split('/').collect();
    let [launcher, dist, platform, fname] = parts.as_slice() else {
        return None;
    };
    if *launcher != "launcher" || *dist != "dist" || platform.is_empty() {
        return None;
    }
    let (_, pid) = fname.rsplit_once(".old-")?;
    if pid.is_empty() || !pid.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let stem =
        crate::services::binary_freshness::canonical_path_for_backup(Path::new(fname))?;
    let canonical = format!("launcher/dist/{}/{}", platform, stem.to_str()?);
    tracked_at_head.contains(&canonical).then_some(canonical)
}

/// Split a `-z` git listing into paths.
fn nul_paths(raw: &str) -> Vec<String> {
    raw.split('\0')
        .filter(|p| !p.is_empty())
        .map(str::to_string)
        .collect()
}

/// What the `-wip` snapshot must NOT record (W3R-03): every launcher rename
/// artefact among the untracked files, and — when the artefact's canonical
/// file is missing from disk because it was renamed — that canonical path
/// too, so the snapshot keeps HEAD's copy instead of recording a deletion.
async fn rename_artefact_exclusions(
    root: &Path,
    untracked: &[String],
) -> Result<Vec<String>, String> {
    if !untracked.iter().any(|p| p.starts_with("launcher/dist/")) {
        return Ok(Vec::new());
    }
    let tracked: std::collections::HashSet<String> = nul_paths(
        &backup_git(
            root,
            &["ls-tree", "-r", "--name-only", "-z", "HEAD", "--", "launcher/dist"],
            &[],
        )
        .await?,
    )
    .into_iter()
    .collect();
    let mut out: Vec<String> = Vec::new();
    for rel in untracked {
        if let Some(canonical) = launcher_rename_artefact_canonical(rel, &tracked) {
            out.push(rel.clone());
            if !root.join(&canonical).exists() && !out.contains(&canonical) {
                out.push(canonical);
            }
        }
    }
    Ok(out)
}

/// Remove what a failed backup had already created (W3R-17), so a refusal
/// leaves no half-made `vco-backup/*` branch or unverified bundle behind and a
/// retry is not blocked by "branch already exists". Best effort; the returned
/// sentence says what was (or could not be) removed.
async fn discard_partial_backup(root: &Path, branches: &[String], bundle: Option<&Path>) -> String {
    let mut removed = Vec::new();
    let mut left = Vec::new();
    for b in branches {
        match backup_git(root, &["branch", "-D", b.as_str()], &[]).await {
            Ok(_) => removed.push(b.clone()),
            Err(_) => left.push(b.clone()),
        }
    }
    if let Some(p) = bundle {
        if p.exists() {
            match std::fs::remove_file(p) {
                Ok(()) => removed.push(p.display().to_string()),
                Err(_) => left.push(p.display().to_string()),
            }
        }
    }
    match (removed.is_empty(), left.is_empty()) {
        (true, true) => String::new(),
        (false, true) => format!(" The partial backup ({}) was removed.", removed.join(", ")),
        _ => format!(
            " The partial backup could not be fully removed: {} remain(s).",
            left.join(", ")
        ),
    }
}

/// Save everything `git reset --hard <upstream_ref>` could destroy, BEFORE it
/// runs — and before any merge/rebase abort, which can itself discard hand
/// edits (owner ruling F-W2-03 + reviews W2R-03, W3R-01). Nothing in the
/// working tree or the index is modified:
///
/// 1. `vco-backup/<stamp>` → the current HEAD (the local commits).
/// 2. `vco-backup/<stamp>-branch` → the tip of `update_branch` when HEAD is
///    NOT on it (a detached HEAD, a rebase in progress) and that tip holds
///    commits neither HEAD nor upstream has (W3R-02: the reset moves that
///    branch, so its commits must be saved too).
/// 3. `vco-backup/<stamp>-wip` → a commit of the WHOLE working tree — tracked
///    modifications, deletions, conflict-marked files AND untracked
///    (non-ignored) files — built in a throwaway index (`GIT_INDEX_FILE`), so
///    the user's index and files stay exactly as they are. Only when the tree
///    differs from HEAD. The launcher's own Windows rename artefacts are
///    excluded ([`launcher_rename_artefact_canonical`], W3R-03). (Chosen over
///    `git stash push --include-untracked`, which REMOVES untracked files from
///    disk — files the reset itself would leave alone.)
/// 4. `<backups_dir>/orchestrator-reset-<stamp>.bundle` holding those refs'
///    commits that upstream lacks, then `git bundle verify`.
///
/// Any failure is an `Err` and the caller REFUSES the reset; whatever this
/// call had already created is removed first (W3R-17). With no local commit
/// and a clean tree there is nothing to lose: the HEAD branch is still
/// created, no bundle is written (git refuses an empty bundle). `progress`
/// receives one line naming what is being saved (the snapshot has no size
/// bound — it saves everything — so the user is told how much).
pub(crate) async fn create_reset_backup(
    root: &Path,
    upstream_ref: &str,
    update_branch: &str,
    backups_dir: &Path,
    stamp: &str,
    progress: &(dyn Fn(&str) + Send + Sync),
) -> Result<ResetBackup, String> {
    let head = backup_git(root, &["rev-parse", "--verify", "HEAD"], &[]).await?;
    let not_upstream = format!("^{}", upstream_ref);

    // The update branch's own tip, when HEAD is not on it and it holds
    // commits that neither HEAD nor upstream has.
    let branch_ref = format!("refs/heads/{}", update_branch);
    let tip = match backup_git(root, &["rev-parse", "--verify", "--quiet", branch_ref.as_str()], &[])
        .await
    {
        Ok(t) if !t.is_empty() && t != head => {
            let not_head = format!("^{}", head);
            let only_there: u32 = backup_git(
                root,
                &["rev-list", "--count", t.as_str(), not_head.as_str(), not_upstream.as_str()],
                &[],
            )
            .await?
            .parse()
            .map_err(|e| format!("could not count the branch's own commits: {}", e))?;
            (only_there > 0).then_some(t)
        }
        _ => None,
    };
    let mut count_args = vec!["rev-list", "--count", head.as_str()];
    if let Some(t) = &tip {
        count_args.push(t.as_str());
    }
    count_args.push(not_upstream.as_str());
    let local_commits: u32 = backup_git(root, &count_args, &[])
        .await?
        .parse()
        .map_err(|e| format!("could not count the local commits: {}", e))?;

    // What the snapshot will hold, and what it must leave out.
    let untracked = nul_paths(
        &backup_git(root, &["ls-files", "--others", "--exclude-standard", "-z"], &[]).await?,
    );
    let exclusions = rename_artefact_exclusions(root, &untracked).await?;
    let captured: Vec<&String> = untracked.iter().filter(|p| !exclusions.contains(p)).collect();
    let bytes: u64 = captured
        .iter()
        .filter_map(|p| std::fs::metadata(root.join(p)).ok())
        .map(|m| m.len())
        .sum();
    progress(&format!(
        "Saving your work before the reset: {} local commit(s), uncommitted changes and {} \
         untracked file(s) ({})...",
        local_commits,
        captured.len(),
        vct_launcher_core::units::human_bytes(bytes)
    ));

    // The working-tree snapshot, through a throwaway index.
    let scratch = tempfile::tempdir().map_err(|e| format!("scratch dir: {}", e))?;
    let index = scratch.path().join("index").to_string_lossy().to_string();
    let env = [("GIT_INDEX_FILE", index.as_str())];
    backup_git(root, &["read-tree", head.as_str()], &env).await?;
    let excludes: Vec<String> = exclusions
        .iter()
        .map(|p| format!(":(exclude,literal){}", p))
        .collect();
    let mut add_args = vec!["add", "--all", "--", "."];
    add_args.extend(excludes.iter().map(String::as_str));
    backup_git(root, &add_args, &env).await?;
    let tree = backup_git(root, &["write-tree"], &env).await?;
    let head_tree = format!("{}^{{tree}}", head);
    let head_tree = backup_git(root, &["rev-parse", head_tree.as_str()], &[]).await?;
    let wip = if tree != head_tree {
        let msg = format!(
            "vco-backup {}: uncommitted and untracked changes before the reset",
            stamp
        );
        Some(
            backup_git(
                root,
                &["commit-tree", tree.as_str(), "-p", head.as_str(), "-m", msg.as_str()],
                &[],
            )
            .await?,
        )
    } else {
        None
    };

    // Refs and bundle; on any failure, remove what was created.
    let branch = format!("vco-backup/{}", stamp);
    let mut created: Vec<String> = Vec::new();
    let mut bundle_written: Option<PathBuf> = None;
    let saved = save_backup_refs_and_bundle(
        root,
        backups_dir,
        stamp,
        &branch,
        &head,
        tip.as_deref(),
        wip.as_deref(),
        local_commits,
        upstream_ref,
        &mut created,
        &mut bundle_written,
    )
    .await;
    match saved {
        Ok(backup) => Ok(backup),
        Err(e) => {
            let cleanup = discard_partial_backup(root, &created, bundle_written.as_deref()).await;
            Err(format!("{}.{}", e, cleanup))
        }
    }
}

/// [`create_reset_backup`]'s ref + bundle half. Records every branch it
/// creates in `created` and the bundle path in `bundle_written` BEFORE the
/// step that could fail after it, so the caller can undo exactly that.
#[allow(clippy::too_many_arguments)]
async fn save_backup_refs_and_bundle(
    root: &Path,
    backups_dir: &Path,
    stamp: &str,
    branch: &str,
    head: &str,
    tip: Option<&str>,
    wip: Option<&str>,
    local_commits: u32,
    upstream_ref: &str,
    created: &mut Vec<String>,
    bundle_written: &mut Option<PathBuf>,
) -> Result<ResetBackup, String> {
    backup_git(root, &["branch", branch, head], &[]).await?;
    created.push(branch.to_string());
    let tip_branch = match tip {
        Some(sha) => {
            let b = format!("{}-branch", branch);
            backup_git(root, &["branch", b.as_str(), sha], &[]).await?;
            created.push(b.clone());
            Some(b)
        }
        None => None,
    };
    let wip_branch = match wip {
        Some(sha) => {
            let b = format!("{}-wip", branch);
            backup_git(root, &["branch", b.as_str(), sha], &[]).await?;
            created.push(b.clone());
            Some(b)
        }
        None => None,
    };
    if local_commits == 0 && wip_branch.is_none() {
        return Ok(ResetBackup {
            branch: branch.to_string(),
            uncommitted_branch: None,
            branch_tip: None,
            bundle: None,
            local_commits,
        });
    }

    // v0.2.100 F-W3-13: create + verify (+ removing a partial file) is the
    // ONE Python home `vco_lib.git_bundle_backup`, shared with
    // `vco_lib.hard_cut`. The refs above stay here: they are the reset's own
    // rescue points, not part of the bundle sequence.
    let name = format!("orchestrator-reset-{}.bundle", stamp);
    let exclude = format!("^{}", upstream_ref);
    let mut refs: Vec<String> = vec![branch.to_string()];
    refs.extend([&tip_branch, &wip_branch].into_iter().flatten().cloned());
    refs.push(exclude);
    *bundle_written = Some(backups_dir.join(&name));
    let (repo, dir) = (root.to_path_buf(), backups_dir.to_path_buf());
    let bundle = tokio::task::spawn_blocking(move || {
        let refs: Vec<&str> = refs.iter().map(String::as_str).collect();
        crate::services::vco_lib_bridge::create_verified_bundle(&repo, &dir, &name, &refs)
    })
    .await
    .map_err(|e| format!("the bundle backup task failed: {}", e))??;
    Ok(ResetBackup {
        branch: branch.to_string(),
        uncommitted_branch: wip_branch,
        branch_tip: tip_branch,
        bundle: Some(bundle),
        local_commits,
    })
}

/// The branch `ResetHard` lands on (W3R-02). The attached branch; for a
/// detached HEAD in a rebase, the branch the rebase is rewriting (its
/// `head-name`); otherwise the update branch every surface uses for a
/// detached HEAD (`resolve_branch`'s name — [`git_cmd::FALLBACK_BRANCH`]).
///
/// [`git_cmd::FALLBACK_BRANCH`]: crate::commands::git_cmd::FALLBACK_BRANCH
fn reset_branch(root: &Path, state: &crate::commands::git_cmd::BranchState) -> String {
    if !state.detached {
        return state.name.clone();
    }
    for dir in ["rebase-merge", "rebase-apply"] {
        if let Ok(s) = std::fs::read_to_string(root.join(".git").join(dir).join("head-name")) {
            if let Some(b) = s.trim().strip_prefix("refs/heads/") {
                if !b.is_empty() {
                    return b.to_string();
                }
            }
        }
    }
    state.name.clone()
}

/// `ResetHard`'s git operation: identity re-asserted, fetch, the local work
/// saved and verified ([`create_reset_backup`] — a failure REFUSES the reset),
/// THEN any in-progress merge/rebase aborted (a reset does not clear them; an
/// abort that fails, or leaves the state behind, REFUSES the reset — W3R-01),
/// then `git reset --hard vco_upstream/<branch>` with HEAD left ATTACHED to
/// `<branch>` (W3R-02). Failures leave the restore to the driver
/// (`restored: false`).
pub(crate) async fn reset_hard_git_op(
    root: &Path,
    backups_dir: &Path,
    progress: &(dyn Fn(&str) + Send + Sync),
) -> Result<GitOpOutcome, GitOpFailure> {
    reset_hard_git_op_with(root, backups_dir, progress, |p: PathBuf| async move {
        crate::commands::installer::abort_merge_or_rebase_unclaimed(&p).await
    })
    .await
}

/// [`reset_hard_git_op`] with the merge/rebase abort injected — the seam the
/// ordering (backup before abort) and the abort-failed leg are tested through.
pub(crate) async fn reset_hard_git_op_with<A, F>(
    root: &Path,
    backups_dir: &Path,
    progress: &(dyn Fn(&str) + Send + Sync),
    abort: A,
) -> Result<GitOpOutcome, GitOpFailure>
where
    A: FnOnce(PathBuf) -> F + Send,
    F: Future<Output = Result<(), String>> + Send,
{
    use crate::commands::git_user_editable_merge::merge_or_rebase_in_progress;
    let fail = |error: UpdateSurfaceError| GitOpFailure {
        error,
        restored: false,
    };
    if let Some(refusal) = reset_hard_identity_refusal(root) {
        return Err(fail(refusal));
    }
    let state = crate::commands::git_cmd::resolve_branch(root)
        .await
        .map_err(|e| {
            fail(UpdateSurfaceError::Raw(format!(
                "git rev-parse failed: {}",
                e
            )))
        })?;
    let branch = reset_branch(root, &state);
    crate::commands::upstream_fetch::serialized_fetch_upstream(
        root,
        crate::commands::upstream_fetch::FetchPolicy::Quick,
        Some(&branch),
    )
    .await
    .map_err(|e| {
        fail(UpdateSurfaceError::Raw(format!(
            "fetching upstream failed: {}",
            e
        )))
    })?;
    let target = format!(
        "{}/{}",
        crate::commands::self_update::VCO_UPSTREAM_REMOTE,
        branch
    );
    let stamp = chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let backup = create_reset_backup(root, &target, &branch, backups_dir, &stamp, progress)
        .await
        .map_err(|e| {
            fail(UpdateSurfaceError::Refused {
                code: "reset_backup_failed",
                reason: format!(
                    "Refusing `git reset --hard`: your local commits and changes could not be \
                     backed up first ({}). Nothing was reset.",
                    e
                ),
            })
        })?;
    tracing::info!("[vct] {}: reset backup: {}", SURFACE, backup.describe());

    // Only now — with the work saved — conclude an in-progress merge/rebase.
    // A reset over one leaves the clone wedged (`reset --hard` does not clear
    // `.git/rebase-merge`), so an abort that fails, or that leaves the state
    // behind, refuses the reset instead of resetting anyway.
    let in_progress = merge_or_rebase_in_progress(root);
    if let Some(op) = in_progress {
        progress(&format!("Aborting the {} in progress...", op));
    }
    if let Err(e) = abort(root.to_path_buf()).await {
        return Err(fail(UpdateSurfaceError::Refused {
            code: "reset_abort_failed",
            reason: format!(
                "Refusing `git reset --hard`: the {} in progress could not be aborted ({}), and \
                 resetting over it would leave the clone stuck in it. Nothing was reset. {} \
                 Conclude it by hand (`git merge --abort` or `git rebase --abort` in {}), then \
                 try again.",
                in_progress.unwrap_or("merge/rebase"),
                e,
                backup.describe(),
                root.display()
            ),
        }));
    }
    if let Some(op) = merge_or_rebase_in_progress(root) {
        return Err(fail(UpdateSurfaceError::Refused {
            code: "reset_state_not_clear",
            reason: format!(
                "Refusing `git reset --hard`: a {} is still in progress in {} after the abort, and \
                 resetting over it would leave the clone stuck in it. Nothing was reset. {}",
                op,
                root.display(),
                backup.describe()
            ),
        }));
    }

    crate::commands::update_pipeline::clear_prior_resume_state(root);
    let out = crate::commands::git_cmd::run_git_raw(root, &["reset", "--hard", target.as_str()])
        .await
        .map_err(|e| fail(UpdateSurfaceError::Raw(e)))?;
    if !out.status.success() {
        return Err(fail(UpdateSurfaceError::Raw(format!(
            "git reset --hard {} failed: {}. {}",
            target,
            String::from_utf8_lossy(&out.stderr).trim(),
            backup.describe()
        ))));
    }
    // A detached HEAD was moved, not the branch: point `<branch>` here and
    // attach to it, so the next PullFf advances the branch (the old branch
    // tip is in the backup when it held anything).
    let now = crate::commands::git_cmd::resolve_branch(root).await.ok();
    if now.as_ref().is_none_or(|s| s.detached || s.name != branch) {
        let out = crate::commands::git_cmd::run_git_raw(root, &["checkout", "-B", branch.as_str()])
            .await
            .map_err(|e| fail(UpdateSurfaceError::Raw(e)))?;
        let attached = crate::commands::git_cmd::resolve_branch(root)
            .await
            .is_ok_and(|s| !s.detached && s.name == branch);
        if !out.status.success() || !attached {
            return Err(fail(UpdateSurfaceError::Raw(format!(
                "the reset to {} landed but HEAD could not be re-attached to branch {}: {}. Run \
                 `git checkout -B {} {}` in {}. {}",
                target,
                branch,
                String::from_utf8_lossy(&out.stderr).trim(),
                branch,
                target,
                root.display(),
                backup.describe()
            ))));
        }
        tracing::info!(
            "[vct] {}: HEAD was detached; re-attached to branch {} at {}",
            SURFACE,
            branch,
            target
        );
    }
    Ok(GitOpOutcome {
        already_up_to_date: false,
        dist_binary_stale: false,
        branch,
        reset_backup: Some(backup),
    })
}

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

/// THE orchestrator update. Every surface reaches this (WP-03b migrates the
/// legacy commands onto it). `window` streams progress to the modal whenever
/// one exists; `surface` names the caller in logs.
pub(crate) async fn run_update<R: Runtime>(
    app: AppHandle<R>,
    window: Option<&Window>,
    kind: UpdateKind,
    surface: &'static str,
) -> Result<UpdateOutcome, UpdateSurfaceError> {
    tracing::info!(
        "[vct] {}: {:?} update requested by {}",
        SURFACE,
        kind,
        surface
    );
    let mut ops = LiveOps::new(app, window, None);
    run_update_with(&mut ops, kind).await
}

/// [`run_update`] for a caller that ALREADY holds the orchestrator-update
/// single-flight claim — the conflict resolvers (`keep_local_…`,
/// `accept_upstream_…`, `resolve_autostash_pop_and_retry`,
/// `resolve_untracked_collision_and_retry`) do destructive git work under it
/// and hand it over, so ONE claim spans that work and the update it unblocks.
pub(crate) async fn run_update_claimed<R: Runtime>(
    app: AppHandle<R>,
    window: Option<&Window>,
    kind: UpdateKind,
    surface: &'static str,
    flight: crate::commands::single_flight::SingleFlightGuard,
) -> Result<UpdateOutcome, UpdateSurfaceError> {
    tracing::info!(
        "[vct] {}: {:?} update requested by {} (claim handed over)",
        SURFACE,
        kind,
        surface
    );
    let mut ops = LiveOps::new(app, window, Some(flight));
    run_update_with(&mut ops, kind).await
}

/// [`run_update`] over any [`UpdateOps`] — logs the ledger either way.
pub(crate) async fn run_update_with<O: UpdateOps + Send>(
    ops: &mut O,
    kind: UpdateKind,
) -> Result<UpdateOutcome, UpdateSurfaceError> {
    let mut ledger = PhaseLedger::default();
    let result = drive(ops, kind, &mut ledger).await;
    tracing::info!(
        "[vct] {}: phase ledger: {}",
        SURFACE,
        serde_json::to_string(&ledger).unwrap_or_default()
    );
    result
}

/// The ONE update command. `kind` is the variant name (`"PullFf"`, …). On
/// failure the `Err` string is the JSON contract of
/// `update_failure::UpdateSurfaceError::to_json` — `{kind, message, …}`,
/// `message` never empty and never prefixed.
#[command]
pub async fn run_orchestrator_update<R: Runtime>(
    app: AppHandle<R>,
    window: Window,
    kind: UpdateKindDto,
) -> Result<UpdateOutcome, String> {
    run_update(app, Some(&window), kind, SURFACE)
        .await
        .map_err(|e| e.to_json())
}

// ---------------------------------------------------------------------------
// F-W3-14: a conflict resolver acts only on the root the pipeline updates
// ---------------------------------------------------------------------------

/// Does the GUI-supplied `gui` path name the same directory as the resolved
/// install `root`? Compared after canonicalisation (symlinks, `..`, the
/// Windows verbatim prefix); an unresolvable GUI path never matches. Pure
/// apart from the filesystem reads.
pub(crate) fn gui_path_matches_root(gui: &Path, root: &Path) -> bool {
    use vct_launcher_core::services::install_root::strip_windows_verbatim_prefix;
    let canon = |p: &Path| -> Option<String> {
        dunce::canonicalize(p).ok().map(|c| strip_windows_verbatim_prefix(&c.to_string_lossy()))
    };
    match (canon(gui), canon(root)) {
        (Some(a), Some(b)) => a == b,
        _ => false,
    }
}

/// The typed refusal for a GUI path that is not the resolved root.
pub(crate) fn check_gui_path(gui: &Path, root: &Path) -> Result<(), UpdateSurfaceError> {
    if gui_path_matches_root(gui, root) {
        return Ok(());
    }
    Err(UpdateSurfaceError::Refused {
        code: "path_not_install_root",
        reason: format!(
            "the conflict was reported for {}, but this launcher updates {} — nothing was \
             changed. Resolve it from the launcher that belongs to {} (or run `python install.py \
             --update` there).",
            gui.display(),
            root.display(),
            gui.display()
        ),
    })
}

/// F-W3-14 (v0.2.100 WP-06): every conflict resolver does git/file work on
/// the GUI-supplied `path` and then hands its claim to the ONE pipeline,
/// which updates the RESOLVED install root. If the two differ, the
/// resolution would change one tree and the update another. So each
/// resolver command calls this as its FIRST statement — before any git or
/// file work — and refuses on a mismatch with the pipeline's typed
/// `Refused` JSON. `db = None` (no launcher database) refuses too.
pub(crate) fn require_gui_path_is_resolved_root(
    db: Option<&crate::db::Db>,
    gui_path: &str,
) -> Result<PathBuf, String> {
    let refused = |code: &'static str, reason: String| UpdateSurfaceError::Refused { code, reason }.to_json();
    let db = db.ok_or_else(|| {
        refused(
            "no_database",
            "The launcher database is not available, so the install root cannot be resolved. \
             Restart the launcher and try again."
                .into(),
        )
    })?;
    let root = crate::commands::installer::resolve_root_with_db(db)
        .map_err(|e| refused("root_unresolved", e.to_string()))?
        .path;
    check_gui_path(Path::new(gui_path), &root).map_err(|e| e.to_json())?;
    Ok(root)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A recording fake: every op appends its name; outcomes are scripted.
    struct FakeOps {
        calls: Vec<String>,
        audits: Vec<(String, Value)>,
        root: Result<PathBuf, UpdateSurfaceError>,
        git: bool,
        python: Result<String, String>,
        tree: Option<UpdateSurfaceError>,
        precheck: Result<(), String>,
        pin: Result<(), String>,
        hub_stop: Result<PrePullRenames, String>,
        git_op: Result<GitOpOutcome, GitOpFailure>,
        head: Result<(), String>,
        install: Result<InstallPyRun, String>,
        binary: BinaryCheck,
        relaunch: Result<RelaunchOutcome, RelaunchError>,
        /// When set, Merge/Rebase/Resume run their REAL git operation on this
        /// temp repo (everything else stays fake — install.py included).
        real_repo: Option<PathBuf>,
    }

    fn ok_run() -> InstallPyRun {
        InstallPyRun {
            success: true,
            stderr: String::new(),
            exit_code: Some(0),
            signal: None,
            last_step: Some("[10/10]".into()),
            stdout_tail: String::new(),
        }
    }

    impl FakeOps {
        fn happy() -> Self {
            Self {
                calls: vec![],
                audits: vec![],
                root: Ok(PathBuf::from("/fake/root")),
                git: true,
                python: Ok("python3".into()),
                tree: None,
                precheck: Ok(()),
                pin: Ok(()),
                hub_stop: Ok(PrePullRenames::default()),
                git_op: Ok(GitOpOutcome {
                    already_up_to_date: false,
                    dist_binary_stale: false,
                    branch: "main".into(),
                    reset_backup: None,
                }),
                head: Ok(()),
                install: Ok(ok_run()),
                binary: BinaryCheck::Ready,
                relaunch: Ok(RelaunchOutcome::Spawned {
                    exe: PathBuf::from("/fake/root/launcher/dist/x/vct-launcher"),
                }),
                real_repo: None,
            }
        }
        fn rec(&mut self, name: &str) {
            self.calls.push(name.to_string());
        }
        fn called(&self, name: &str) -> bool {
            self.calls.iter().any(|c| c == name)
        }
        fn failure_rows(&self) -> Vec<&Value> {
            self.audits
                .iter()
                .filter(|(op, d)| op == "update_orchestrator_complete" && d["success"] == false)
                .map(|(_, d)| d)
                .collect()
        }
    }

    /// The ops that change something outside the launcher process's memory.
    const MUTATING: [&str; 10] = [
        "arm_gate",
        "stop_hub_and_rename",
        "git_op",
        "abort_restore",
        "run_install_py",
        "refresh_after_install",
        "restart_hub",
        "bookkeeping",
        "relaunch",
        "pin_remote",
    ];

    impl UpdateOps for FakeOps {
        fn resolve_root(&mut self) -> Result<PathBuf, UpdateSurfaceError> {
            self.rec("resolve_root");
            self.root.clone()
        }
        fn claim(&mut self, _root: &Path) -> Result<(), UpdateSurfaceError> {
            self.rec("claim");
            Ok(())
        }
        fn git_available(&mut self) -> impl Future<Output = bool> + Send {
            self.rec("git_available");
            let v = self.git;
            async move { v }
        }
        fn python_for_install(
            &mut self,
            _root: &Path,
        ) -> impl Future<Output = Result<String, String>> + Send {
            self.rec("python_for_install");
            let v = self.python.clone();
            async move { v }
        }
        fn tree_refusal(
            &mut self,
            _root: &Path,
            _kind: UpdateKind,
        ) -> impl Future<Output = Option<UpdateSurfaceError>> + Send {
            self.rec("tree_refusal");
            let v = self.tree.clone();
            async move { v }
        }
        fn hub_stop_precheck(&mut self) -> Result<(), String> {
            self.rec("hub_stop_precheck");
            self.precheck.clone()
        }
        fn pin_remote(&mut self, _root: &Path) -> impl Future<Output = Result<(), String>> + Send {
            self.rec("pin_remote");
            let v = self.pin.clone();
            async move { v }
        }
        fn arm_gate(&mut self) {
            self.rec("arm_gate");
        }
        fn stop_hub_and_rename(
            &mut self,
            _root: &Path,
            _kind: UpdateKind,
        ) -> Result<PrePullRenames, String> {
            self.rec("stop_hub_and_rename");
            self.hub_stop.clone()
        }
        fn read_head(&mut self, _root: &Path) -> impl Future<Output = Option<String>> + Send {
            let n = self.calls.iter().filter(|c| *c == "read_head").count();
            self.rec("read_head");
            async move { Some(format!("sha{}", n)) }
        }
        fn current_branch(&mut self, _root: &Path) -> impl Future<Output = String> + Send {
            self.rec("current_branch");
            async { "main".to_string() }
        }
        fn git_op(
            &mut self,
            _root: &Path,
            kind: UpdateKind,
            _renames: &PrePullRenames,
            _head_before: Option<String>,
        ) -> impl Future<Output = Result<GitOpOutcome, GitOpFailure>> + Send {
            self.rec("git_op");
            self.rec(&format!("git_op:{:?}", kind));
            let v = self.git_op.clone();
            let real = self.real_repo.clone();
            async move {
                let Some(repo) = real else { return v };
                match recovery_git_op(&repo, kind, None).await {
                    Ok(done) => Ok(GitOpOutcome {
                        already_up_to_date: done.already_up_to_date,
                        dist_binary_stale: false,
                        branch: done.branch,
                        reset_backup: None,
                    }),
                    Err(RecoveryFailure::Surface(error)) => Err(GitOpFailure {
                        error,
                        restored: false,
                    }),
                    Err(RecoveryFailure::Pipeline(e)) => Err(GitOpFailure {
                        error: update_failure::from_pipeline_error(e).0,
                        restored: false,
                    }),
                }
            }
        }
        fn head_advance(
            &mut self,
            _root: &Path,
        ) -> impl Future<Output = Result<(), String>> + Send {
            self.rec("head_advance");
            let v = self.head.clone();
            async move { v }
        }
        fn abort_restore(&mut self, _root: &Path, _renames: &PrePullRenames) {
            self.rec("abort_restore");
        }
        fn run_install_py(
            &mut self,
            _root: &Path,
            _python_cmd: &str,
        ) -> impl Future<Output = Result<InstallPyRun, String>> + Send {
            self.rec("run_install_py");
            let v = match &self.install {
                Ok(r) => Ok(InstallPyRun {
                    success: r.success,
                    stderr: r.stderr.clone(),
                    exit_code: r.exit_code,
                    signal: r.signal,
                    last_step: r.last_step.clone(),
                    stdout_tail: r.stdout_tail.clone(),
                }),
                Err(e) => Err(e.clone()),
            };
            async move { v }
        }
        fn refresh_after_install(
            &mut self,
            _root: &Path,
            _branch: &str,
        ) -> impl Future<Output = ()> + Send {
            self.rec("refresh_after_install");
            async {}
        }
        fn drop_gate(&mut self) {
            self.rec("drop_gate");
        }
        fn restart_hub(&mut self, _root: &Path, _ctx: HubRestartContext) {
            self.rec("restart_hub");
        }
        fn binary_check(
            &mut self,
            _root: &Path,
            _branch: &str,
        ) -> impl Future<Output = BinaryCheck> + Send {
            self.rec("binary_check");
            let v = self.binary.clone();
            async move { v }
        }
        fn bookkeeping(&mut self, _root: &Path) -> impl Future<Output = ()> + Send {
            self.rec("bookkeeping");
            async {}
        }
        fn relaunch(
            &mut self,
            _root: &Path,
        ) -> impl Future<Output = Result<RelaunchOutcome, RelaunchError>> + Send {
            self.rec("relaunch");
            let v = self.relaunch.clone();
            async move { v }
        }
        fn audit(&mut self, operation: &str, detail: Value) {
            self.calls.push(format!("audit:{}", operation));
            self.audits.push((operation.to_string(), detail));
        }
        fn progress(&mut self, _stage: &str, _message: &str, _percentage: f32) {}
    }

    async fn run(
        ops: &mut FakeOps,
        kind: UpdateKind,
    ) -> (Result<UpdateOutcome, UpdateSurfaceError>, PhaseLedger) {
        let mut ledger = PhaseLedger::default();
        let r = drive(ops, kind, &mut ledger).await;
        (r, ledger)
    }

    fn assert_no_mutation(ops: &FakeOps, ctx: &str) {
        for m in MUTATING {
            assert!(
                !ops.called(m),
                "{ctx}: `{m}` ran before/despite a refusal: {:?}",
                ops.calls
            );
        }
    }

    /// PREFLIGHT refuses with NO mutation: a missing interpreter is a typed
    /// refusal before the sweep, the hub stop and the pull (it used to be a
    /// post-pull source-only degrade — L2-F04/D2). The failure row is written.
    #[tokio::test]
    async fn preflight_python_missing_refuses_before_any_mutation() {
        let mut ops = FakeOps::happy();
        ops.python = Err("no Python interpreter was found".into());
        let (r, ledger) = run(&mut ops, UpdateKind::PullFf).await;
        let e = r.expect_err("must refuse");
        assert_eq!(e.kind(), "Refused");
        assert!(matches!(
            e,
            UpdateSurfaceError::Refused {
                code: "python_missing",
                ..
            }
        ));
        assert_no_mutation(&ops, "python missing");
        assert_eq!(ledger.failed_phase(), Some(Phase::Preflight));
        let rows = ops.failure_rows();
        assert_eq!(rows.len(), 1, "exactly one failure row: {:?}", ops.audits);
        assert_eq!(rows[0]["failed_phase"], "preflight");
        assert_eq!(rows[0]["error_kind"], "Refused");
    }

    /// Every preflight refusal leg is mutation-free — git missing, a wedged
    /// tree (every kind), an unreadable hub.pid, and (last, being the one
    /// git-config write) the remote pin: its failure leaves the sweep unrun.
    #[tokio::test]
    async fn every_preflight_refusal_performs_no_mutation() {
        let mut cases: Vec<(&str, FakeOps, UpdateKind)> = vec![];
        let mut o = FakeOps::happy();
        o.git = false;
        cases.push(("git missing", o, UpdateKind::PullFf));
        let mut o = FakeOps::happy();
        o.tree = Some(UpdateSurfaceError::Conflict(
            json!({"event": "orchestrator_update_conflict"}),
        ));
        cases.push(("merge in progress", o, UpdateKind::PullFf));
        let mut o = FakeOps::happy();
        o.precheck = Err("hub.pid unreadable".into());
        cases.push(("hub precheck", o, UpdateKind::PullFf));
        // Every kind refuses a wedged tree before any mutation (WP-03b routed
        // Merge / Rebase / Resume; the refusal is the same phase-3 leg).
        for kind in [UpdateKind::Merge, UpdateKind::Rebase, UpdateKind::Resume] {
            let mut o = FakeOps::happy();
            o.tree = Some(UpdateSurfaceError::Refused {
                code: "resume_not_pending",
                reason: "no resume pending".into(),
            });
            cases.push(("tree refusal", o, kind));
        }
        for (name, mut ops, kind) in cases {
            let (r, ledger) = run(&mut ops, kind).await;
            assert!(r.is_err(), "{name}");
            assert_no_mutation(&ops, name);
            assert_eq!(ledger.failed_phase(), Some(Phase::Preflight), "{name}");
            assert_eq!(ops.failure_rows().len(), 1, "{name}");
        }
        // The pin runs last and is the only write: its failure must still
        // leave every PROCESS/TREE mutation unrun.
        let mut ops = FakeOps::happy();
        ops.pin = Err("remote set-url failed".into());
        let (r, _) = run(&mut ops, UpdateKind::PullFf).await;
        assert!(matches!(
            r,
            Err(UpdateSurfaceError::Refused {
                code: "remote_pin_failed",
                ..
            })
        ));
        for m in MUTATING.iter().filter(|m| **m != "pin_remote") {
            assert!(!ops.called(m), "pin failure: `{m}` ran: {:?}", ops.calls);
        }
    }

    /// The happy path runs all thirteen phases, in order, each exactly once;
    /// the success row lands BEFORE the relaunch (the restart hop exits the
    /// process — L2-F12), and the gate is dropped before the binary check.
    #[tokio::test]
    async fn pull_ff_runs_the_thirteen_phases_in_order() {
        let mut ops = FakeOps::happy();
        let (r, ledger) = run(&mut ops, UpdateKind::PullFf).await;
        let out = r.expect("happy path");
        let phases: Vec<Phase> = ledger.entries.iter().map(|e| e.phase).collect();
        assert_eq!(phases, PHASE_ORDER.to_vec());
        assert!(
            ledger.entries.iter().all(|e| e.status == PhaseStatus::Done),
            "{:?}",
            ledger
        );
        assert!(out.install_py_ran && out.restarted);
        assert_eq!(out.phases, ledger.entries);
        let pos = |n: &str| {
            ops.calls
                .iter()
                .position(|c| c == n)
                .unwrap_or_else(|| panic!("{n} not called"))
        };
        assert!(
            pos("arm_gate") > pos("pin_remote"),
            "sweep after every refusal (L2-F14)"
        );
        assert!(
            pos("drop_gate") < pos("binary_check"),
            "gate dropped before the binary wait"
        );
        assert!(pos("audit:update_orchestrator_complete") < pos("relaunch"));
        assert!(pos("bookkeeping") < pos("relaunch"));
        if !cfg!(windows) {
            assert!(pos("restart_hub") < pos("binary_check"));
        }
        assert!(ops.failure_rows().is_empty());
    }

    /// install.py failure: binaries restored + hub restarted, no relaunch, a
    /// typed InstallFailed whose message carries the exit code, step and log.
    #[tokio::test]
    async fn install_failure_restores_and_reports_the_reason() {
        let mut ops = FakeOps::happy();
        ops.install = Ok(InstallPyRun {
            success: false,
            stderr: String::new(),
            exit_code: None,
            signal: Some(9),
            last_step: Some("[7/10]".into()),
            stdout_tail: "[7/10] Pulling models".into(),
        });
        let (r, ledger) = run(&mut ops, UpdateKind::PullFf).await;
        let e = r.expect_err("install failed");
        assert_eq!(e.kind(), "InstallFailed");
        let msg = e.message();
        assert!(
            msg.contains("signal 9") && msg.contains("[7/10]") && msg.contains("install.jsonl"),
            "{msg}"
        );
        assert!(ops.called("abort_restore"));
        assert!(!ops.called("relaunch") && !ops.called("bookkeeping"));
        assert_eq!(ledger.failed_phase(), Some(Phase::InstallPy));
        assert_eq!(ops.failure_rows().len(), 1);
        // spawn failure: same restore, same kind.
        let mut ops = FakeOps::happy();
        ops.install = Err("install.py --update failed to spawn: No such file".into());
        let (r, _) = run(&mut ops, UpdateKind::PullFf).await;
        assert_eq!(r.unwrap_err().kind(), "InstallFailed");
        assert!(ops.called("abort_restore"));
    }

    /// A git-op failure the pull sequence already restored is NOT restored a
    /// second time; one it did not restore IS.
    #[tokio::test]
    async fn git_op_failure_restores_exactly_once() {
        for restored in [true, false] {
            let mut ops = FakeOps::happy();
            ops.git_op = Err(GitOpFailure {
                error: UpdateSurfaceError::Raw("boom".into()),
                restored,
            });
            let (r, ledger) = run(&mut ops, UpdateKind::ResetHard).await;
            assert!(r.is_err());
            assert_eq!(ops.called("abort_restore"), !restored);
            assert!(!ops.called("run_install_py"));
            assert_eq!(ledger.failed_phase(), Some(Phase::GitOp));
        }
    }

    /// ApplyOnly runs no git operation and no HEAD check, but everything else.
    #[tokio::test]
    async fn apply_only_skips_git_and_runs_install() {
        let mut ops = FakeOps::happy();
        let (r, ledger) = run(&mut ops, UpdateKind::ApplyOnly).await;
        assert!(r.expect("apply only").install_py_ran);
        assert!(!ops.called("git_op") && !ops.called("head_advance"));
        assert!(ops.called("stop_hub_and_rename") && ops.called("run_install_py"));
        let status = |p: Phase| ledger.entries.iter().find(|e| e.phase == p).unwrap().status;
        assert_eq!(status(Phase::GitOp), PhaseStatus::Skipped);
        assert_eq!(status(Phase::HeadAdvance), PhaseStatus::Skipped);
    }

    /// Already up to date: no install.py, the gate is still dropped, and a
    /// relaunch is attempted only when the dist binary is newer.
    #[tokio::test]
    async fn already_up_to_date_skips_install_and_drops_the_gate() {
        for stale in [false, true] {
            let mut ops = FakeOps::happy();
            ops.git_op = Ok(GitOpOutcome {
                already_up_to_date: true,
                dist_binary_stale: stale,
                branch: "main".into(),
                reset_backup: None,
            });
            let (r, ledger) = run(&mut ops, UpdateKind::PullFf).await;
            let out = r.expect("up to date is success");
            assert!(!out.install_py_ran && !ops.called("run_install_py"));
            assert!(ops.called("drop_gate"));
            assert_eq!(ops.called("relaunch"), stale);
            assert_eq!(ledger.entries.len(), 13, "every phase is accounted for");
        }
    }

    /// A relaunch refusal (dist not newer) does not fail the applied update.
    #[tokio::test]
    async fn relaunch_refusal_is_reported_not_failed() {
        let mut ops = FakeOps::happy();
        ops.relaunch = Err(RelaunchError::Refused(RelaunchRefusal::NotNewer {
            running: "0.2.100".into(),
            dist: "0.2.100".into(),
        }));
        let (r, ledger) = run(&mut ops, UpdateKind::PullFf).await;
        let out = r.expect("applied update succeeds");
        assert!(!out.restarted);
        assert!(out.message.contains("not restarted"), "{}", out.message);
        assert_eq!(ledger.entries.last().unwrap().status, PhaseStatus::Skipped);
        assert!(ops.failure_rows().is_empty());
    }

    /// Phase 11's pure decision, both directions (AD-8 tri-state), from the
    /// three versions alone — no poll, no `git pull` (L2-F11).
    #[test]
    fn decide_binary_refresh_table() {
        let d = decide_binary_refresh;
        assert_eq!(d("0.2.100", Some("0.2.100"), None, None), BinaryCheck::RunningCurrent);
        // A running binary AHEAD of source is current too — never "restart".
        assert_eq!(d("0.2.100", Some("0.2.99"), Some("0.2.99"), None), BinaryCheck::RunningCurrent);
        assert_eq!(d("0.2.99", Some("0.2.100"), Some("0.2.100"), None), BinaryCheck::Ready);
        assert_eq!(
            d("0.2.99", Some("0.2.100"), Some("0.2.100"), Some("0.2.100")),
            BinaryCheck::Ready
        );
        // A dist AHEAD of source satisfies it (the v0.2.48 pin-stale case).
        assert_eq!(d("0.2.9", Some("0.2.9"), Some("0.2.10"), None), BinaryCheck::RunningCurrent);
        assert_eq!(d("0.2.8", Some("0.2.9"), Some("0.2.10"), None), BinaryCheck::Ready);
        assert!(matches!(
            d("0.2.98", Some("0.2.100"), Some("0.2.99"), None),
            BinaryCheck::Partial { .. }
        ));
        assert!(matches!(
            d("0.2.99", Some("0.2.100"), Some("0.2.99"), None),
            BinaryCheck::NotPublished { .. }
        ));
        assert!(matches!(
            d("0.2.99", Some("0.2.100"), None, None),
            BinaryCheck::NotPublished { .. }
        ));
        assert!(matches!(d("0.2.99", None, None, None), BinaryCheck::Unknown { .. }));
        assert!(matches!(
            d("0.2.99", Some("0.2.100-rc1"), None, None),
            BinaryCheck::Unknown { .. }
        ));
        assert!(matches!(
            d("0.2.99", Some("0.2.100"), Some("garbage"), None),
            BinaryCheck::Unknown { .. }
        ));
    }

    /// W2R-02: a launcher dist AT source with a LAGGING hub sidecar is not
    /// "the launcher is below the source" — it is `HubLagging`, whose note
    /// names the hub, and no launcher-divergence verdict. Leave-alone: an
    /// empty/absent hub sidecar never blocks, a caught-up hub is Ready, and a
    /// genuinely older launcher dist is still `Partial`.
    #[test]
    fn a_lagging_hub_is_named_as_the_hub_not_a_partial_launcher() {
        let d = decide_binary_refresh;
        assert_eq!(
            d("0.2.99", Some("0.2.100"), Some("0.2.100"), Some("0.2.99")),
            BinaryCheck::HubLagging {
                hub: "0.2.99".into(),
                source: "0.2.100".into()
            }
        );
        assert_eq!(d("0.2.99", Some("0.2.100"), Some("0.2.100"), Some("")), BinaryCheck::Ready);
        assert_eq!(
            d("0.2.99", Some("0.2.100"), Some("0.2.100"), Some("0.2.101")),
            BinaryCheck::Ready
        );
        assert!(matches!(
            d("0.2.98", Some("0.2.100"), Some("0.2.99"), Some("0.2.99")),
            BinaryCheck::Partial { .. }
        ));
    }

    /// The ledger/message for a lagging hub says the HUB is behind — never
    /// "the launcher binary on disk (vX) is below the source (vX)".
    #[tokio::test]
    async fn hub_lagging_note_names_the_hub() {
        let mut ops = FakeOps::happy();
        ops.binary = BinaryCheck::HubLagging {
            hub: "0.2.99".into(),
            source: "0.2.100".into(),
        };
        let (r, _) = run(&mut ops, UpdateKind::PullFf).await;
        let out = r.expect("applied");
        assert!(out.message.contains("vct-hub binary on disk (v0.2.99)"), "{}", out.message);
        assert!(!out.message.contains("launcher binary on disk"), "{}", out.message);
    }

    /// ResetHard's identity assertion: ACT (a non-clone is refused) and
    /// LEAVE-ALONE (a real clone passes).
    #[test]
    fn reset_hard_requires_an_orchestrator_clone() {
        let td = tempfile::tempdir().unwrap();
        assert!(matches!(
            reset_hard_identity_refusal(td.path()),
            Some(UpdateSurfaceError::Refused {
                code: "reset_target_not_orchestrator",
                ..
            })
        ));
        std::fs::write(
            td.path().join("vct-module.json"),
            r#"{"id": "some-module"}"#,
        )
        .unwrap();
        assert!(
            reset_hard_identity_refusal(td.path()).is_some(),
            "a module is not the orchestrator"
        );
        std::fs::write(
            td.path().join("vct-module.json"),
            r#"{"id": "orchestrator"}"#,
        )
        .unwrap();
        assert!(reset_hard_identity_refusal(td.path()).is_none());
    }

    /// The command argument's wire form is the variant name.
    #[test]
    fn update_kind_wire_form_is_the_variant_name() {
        let f: Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/update_failure_messages.json"
        ))
        .unwrap();
        for k in f["contract"]["update_kinds"].as_array().unwrap() {
            let kind: UpdateKind = serde_json::from_value(k.clone()).expect("fixture kind parses");
            assert_eq!(serde_json::to_value(kind).unwrap(), *k);
        }
        let phases: Vec<Value> = PHASE_ORDER
            .iter()
            .map(|p| serde_json::to_value(p).unwrap())
            .collect();
        let fixture_phases: Vec<Value> = f["contract"]["outcome_example"]["phases"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["phase"].clone())
            .collect();
        assert_eq!(
            phases, fixture_phases,
            "the fixture's phase names are the ledger's"
        );
    }

    /// The success value's wire shape is the fixture's `outcome_example`
    /// (same keys, same phase names) — what WP-08's store reads.
    #[tokio::test]
    async fn outcome_wire_shape_matches_the_fixture_example() {
        let f: Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/update_failure_messages.json"
        ))
        .unwrap();
        let example = &f["contract"]["outcome_example"];
        let mut ops = FakeOps::happy();
        let (r, _) = run(&mut ops, UpdateKind::PullFf).await;
        let got = serde_json::to_value(r.expect("happy")).unwrap();
        let keys = |v: &Value| {
            let mut k: Vec<String> = v.as_object().unwrap().keys().cloned().collect();
            k.sort();
            k
        };
        assert_eq!(keys(&got), keys(example));
        assert_eq!(got["kind"], example["kind"]);
        assert_eq!(got["phases"].as_array().unwrap().len(), 13);
        for (g, e) in got["phases"]
            .as_array()
            .unwrap()
            .iter()
            .zip(example["phases"].as_array().unwrap())
        {
            assert_eq!(keys(g), keys(e));
            assert_eq!(g["phase"], e["phase"]);
        }
    }

    // ---- the allowlist: which #[command]s reach run_update -----------------

    /// `#[command]`s that MUST reach `run_update` (AD-1 revised: exactly one).
    const REACHES_RUN_UPDATE: [&str; 1] = ["run_orchestrator_update"];

    /// The per-surface update commands the one pipeline replaced (WP-03b),
    /// and the retired `update_orchestrator_at` (owner Q1). None may exist as
    /// a `#[command]` nor be registered — the GUI invoke census
    /// (`launcher/src/lib/update-invoke-census.test.ts`) proves no caller.
    const RETIRED: [&str; 9] = [
        "update_orchestrator",
        "merge_orchestrator_with_upstream",
        "rebase_orchestrator_onto_upstream",
        "resume_orchestrator_update",
        "apply_launcher_update",
        "apply_pending_install",
        "force_resync_launcher",
        "update_orchestrator_at",
        "get_cached_update_status_refreshed",
    ];

    const SCANNED: [&str; 3] = [
        "src/commands/update_run.rs",
        "src/commands/installer.rs",
        "src/commands/self_update.rs",
    ];

    /// Blank out comments and string/char literal CONTENTS (keeping offsets),
    /// so a name in a comment or a string cannot satisfy the scan.
    pub(crate) fn code_only(src: &str) -> String {
        let b: Vec<char> = src.chars().collect();
        let mut out = String::with_capacity(src.len());
        let mut i = 0;
        let blank = |c: char| if c == '\n' { '\n' } else { ' ' };
        while i < b.len() {
            let c = b[i];
            let next = b.get(i + 1).copied();
            if c == '/' && next == Some('/') {
                while i < b.len() && b[i] != '\n' {
                    out.push(' ');
                    i += 1;
                }
            } else if c == '/' && next == Some('*') {
                let mut depth = 0;
                while i < b.len() {
                    if b[i] == '/' && b.get(i + 1) == Some(&'*') {
                        depth += 1;
                        out.push_str("  ");
                        i += 2;
                    } else if b[i] == '*' && b.get(i + 1) == Some(&'/') {
                        depth -= 1;
                        out.push_str("  ");
                        i += 2;
                        if depth == 0 {
                            break;
                        }
                    } else {
                        out.push(blank(b[i]));
                        i += 1;
                    }
                }
            } else if c == 'r'
                && (next == Some('#') || next == Some('"'))
                && (i == 0 || !b[i - 1].is_alphanumeric() && b[i - 1] != '_')
            {
                // raw string r#"…"#
                let mut j = i + 1;
                let mut hashes = 0;
                while b.get(j) == Some(&'#') {
                    hashes += 1;
                    j += 1;
                }
                if b.get(j) != Some(&'"') {
                    out.push(c);
                    i += 1;
                    continue;
                }
                for _ in i..=j {
                    out.push(' ');
                }
                i = j + 1;
                loop {
                    if i >= b.len() {
                        break;
                    }
                    if b[i] == '"' && (0..hashes).all(|k| b.get(i + 1 + k) == Some(&'#')) {
                        for _ in 0..=hashes {
                            out.push(' ');
                        }
                        i += 1 + hashes;
                        break;
                    }
                    out.push(blank(b[i]));
                    i += 1;
                }
            } else if c == '"' {
                out.push(' ');
                i += 1;
                while i < b.len() && b[i] != '"' {
                    if b[i] == '\\' {
                        out.push(' ');
                        i += 1;
                    }
                    if i < b.len() {
                        out.push(blank(b[i]));
                        i += 1;
                    }
                }
                out.push(' ');
                i += 1;
            } else if c == '\'' && (next == Some('\\') || b.get(i + 2) == Some(&'\'')) {
                // char literal ('x' or '\n'); a lifetime has neither shape.
                out.push(' ');
                i += 1;
                while i < b.len() && b[i] != '\'' {
                    if b[i] == '\\' {
                        out.push(' ');
                        i += 1;
                    }
                    if i < b.len() {
                        out.push(' ');
                        i += 1;
                    }
                }
                out.push(' ');
                i += 1;
            } else {
                out.push(c);
                i += 1;
            }
        }
        out
    }

    /// `name`'s body (brace-matched, code-only) when `name` is a `#[command]`.
    fn command_body(code: &str, name: &str) -> Option<String> {
        let mut search = 0;
        while let Some(at) = code[search..].find("#[command]") {
            let start = search + at;
            let rest = &code[start..];
            let fn_at = rest.find("fn ")?;
            let after = &rest[fn_at + 3..];
            let ident: String = after
                .chars()
                .take_while(|c| c.is_alphanumeric() || *c == '_')
                .collect();
            if ident == name {
                let open = rest[fn_at..].find('{')? + fn_at;
                let mut depth = 0usize;
                for (k, ch) in rest[open..].char_indices() {
                    match ch {
                        '{' => depth += 1,
                        '}' => {
                            depth -= 1;
                            if depth == 0 {
                                return Some(rest[open..open + k + 1].to_string());
                            }
                        }
                        _ => {}
                    }
                }
                return None;
            }
            search = start + "#[command]".len();
        }
        None
    }

    fn scanned_code() -> Vec<(String, String)> {
        SCANNED
            .iter()
            .map(|rel| {
                let p = Path::new(env!("CARGO_MANIFEST_DIR")).join(rel);
                let src = std::fs::read_to_string(&p)
                    .unwrap_or_else(|e| panic!("read {}: {}", p.display(), e));
                (rel.to_string(), code_only(&src))
            })
            .collect()
    }

    fn find_body(files: &[(String, String)], name: &str) -> Option<String> {
        files.iter().find_map(|(_, code)| command_body(code, name))
    }

    /// Identifier tokens of `code` (for exact-name matching).
    fn tokens(code: &str) -> Vec<&str> {
        code.split(|c: char| !(c.is_alphanumeric() || c == '_'))
            .filter(|t| !t.is_empty())
            .collect()
    }

    /// Names of every `#[command]` fn in `code`.
    fn command_names(code: &str) -> Vec<String> {
        let mut out = vec![];
        let mut search = 0;
        while let Some(at) = code[search..].find("#[command]") {
            let start = search + at;
            if let Some(fn_at) = code[start..].find("fn ") {
                let ident: String = code[start + fn_at + 3..]
                    .chars()
                    .take_while(|c| c.is_alphanumeric() || *c == '_')
                    .collect();
                out.push(ident);
            }
            search = start + "#[command]".len();
        }
        out
    }

    /// The ONE update command: exactly the allowlist's `#[command]`s reach
    /// `run_update(` in their own CODE (comments and strings blanked), and it
    /// is registered. Red-proof: delete the `run_update(` call from
    /// `run_orchestrator_update` → red; add a `#[command]` whose body calls
    /// `run_update(` → red.
    #[test]
    fn allowlisted_update_commands_reach_run_update() {
        let files = scanned_code();
        for name in REACHES_RUN_UPDATE {
            let body =
                find_body(&files, name).unwrap_or_else(|| panic!("#[command] {name} not found"));
            assert!(
                body.contains("run_update("),
                "#[command] {name} must reach run_update — body:\n{body}"
            );
        }
        let mut reaching: Vec<String> = files
            .iter()
            .flat_map(|(_, code)| {
                command_names(code)
                    .into_iter()
                    .filter(|n| command_body(code, n).is_some_and(|b| b.contains("run_update(")))
                    .collect::<Vec<_>>()
            })
            .collect();
        reaching.sort();
        assert_eq!(reaching, REACHES_RUN_UPDATE.to_vec(), "exactly one command reaches run_update");
        let lib = code_only(
            &std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lib.rs"))
                .unwrap(),
        );
        assert!(
            lib.contains("commands::update_run::run_orchestrator_update"),
            "run_orchestrator_update must be registered in generate_handler!"
        );
    }

    /// The retired commands are GONE — neither defined as a `#[command]` in
    /// the scanned files nor named anywhere in `lib.rs`'s code (the
    /// `generate_handler!` list). Red-proof: re-add any one of them as a
    /// `#[command]` → red.
    #[test]
    fn retired_update_commands_are_neither_defined_nor_registered() {
        let files = scanned_code();
        let lib = code_only(
            &std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lib.rs"))
                .unwrap(),
        );
        let lib_tokens = tokens(&lib);
        for name in RETIRED {
            for (rel, code) in &files {
                assert!(
                    !command_names(code).iter().any(|n| n == name),
                    "retired #[command] {name} is still defined in {rel}"
                );
            }
            assert!(
                !lib_tokens.contains(&name),
                "retired command {name} is still named in lib.rs code"
            );
        }
    }

    /// WP-03b (F-W2-01/F-W2-06): Merge, Rebase and Resume are ROUTED — each
    /// runs all thirteen phases through the same driver: its git operation
    /// (by kind), the HEAD-advance backstop, install.py and the relaunch.
    #[tokio::test]
    async fn merge_rebase_resume_run_the_one_pipeline() {
        for kind in [UpdateKind::Merge, UpdateKind::Rebase, UpdateKind::Resume] {
            let mut ops = FakeOps::happy();
            let (r, ledger) = run(&mut ops, kind).await;
            let out = r.unwrap_or_else(|e| panic!("{kind:?}: {}", e.message()));
            assert!(out.install_py_ran, "{kind:?}");
            assert!(ops.called(&format!("git_op:{:?}", kind)), "{kind:?}: {:?}", ops.calls);
            for op in ["head_advance", "run_install_py", "relaunch"] {
                assert!(ops.called(op), "{kind:?}: `{op}` did not run");
            }
            let phases: Vec<Phase> = ledger.entries.iter().map(|e| e.phase).collect();
            assert_eq!(phases, PHASE_ORDER.to_vec(), "{kind:?}");
            assert!(
                ledger.entries.iter().all(|e| e.status == PhaseStatus::Done),
                "{kind:?}: {:?}",
                ledger
            );
        }
    }

    /// The scanner itself: a name inside a comment or a string is invisible.
    #[test]
    fn code_only_blanks_comments_and_strings() {
        let src = "fn a() { // run_update(\n let s = \"run_update(\"; /* run_update( */ let c = '{'; x::<'a>(); }";
        let code = code_only(src);
        assert!(!code.contains("run_update("), "{code}");
        assert_eq!(code.len(), src.len());
        let src = "#[command]\npub async fn f<R: Runtime>(a: u8) -> Result<(), String> { if x { y } run_update(a) }";
        assert!(command_body(&code_only(src), "f")
            .unwrap()
            .contains("run_update("));
    }
    // ---- WP-03b: the real git operations, on temp repos --------------------

    /// W3R-03: the artefact matcher — ACT on the exact shape and location,
    /// LEAVE-ALONE on everything else.
    #[test]
    fn launcher_rename_artefact_matcher_is_exact() {
        let tracked: std::collections::HashSet<String> = [
            "launcher/dist/windows-x64/vct-launcher.exe",
            "launcher/dist/windows-x64/vct-hub.exe",
            "launcher/dist/linux-x64/vct-launcher",
        ]
        .into_iter()
        .map(String::from)
        .collect();
        let m = |p: &str| launcher_rename_artefact_canonical(p, &tracked);
        assert_eq!(
            m("launcher/dist/windows-x64/vct-launcher.exe.old-4242").as_deref(),
            Some("launcher/dist/windows-x64/vct-launcher.exe")
        );
        assert_eq!(
            m("launcher/dist/windows-x64/vct-hub.exe.old-1").as_deref(),
            Some("launcher/dist/windows-x64/vct-hub.exe")
        );
        assert!(m("launcher/dist/linux-x64/vct-launcher.old-99").is_some());
        for not_ours in [
            "launcher/dist/linux-x64/vct-launcher.old-may7",
            "launcher/dist/windows-x64/vct-launcher.exe.old-",
            "launcher/dist/windows-x64/vct-launcher.exe.old-12a",
            "launcher/dist/windows-x64/notes.txt.old-12",
            "launcher/dist/vct-launcher.exe.old-12",
            "launcher/dist/windows-x64/sub/vct-launcher.exe.old-12",
            "other/dist/windows-x64/vct-launcher.exe.old-12",
            "vct-launcher.exe.old-12",
        ] {
            assert_eq!(m(not_ours), None, "{not_ours}");
        }
    }

    mod real_git {
        use super::*;
        use crate::commands::git_user_editable_merge::tests::{
            init_repo_pair, push_upstream_change, run_git,
        };
        use crate::commands::update_pipeline::{classify_resume, ResumeVerdict};

        fn git_missing() -> bool {
            std::process::Command::new("git")
                .arg("--version")
                .output()
                .map(|o| !o.status.success())
                .unwrap_or(true)
        }

        fn out(repo: &Path, args: &[&str]) -> String {
            let o = std::process::Command::new("git")
                .args(args)
                .current_dir(repo)
                .output()
                .expect("git");
            String::from_utf8_lossy(&o.stdout).trim().to_string()
        }

        fn head(repo: &Path) -> String {
            out(repo, &["rev-parse", "HEAD"])
        }

        fn commit_local(repo: &Path, file: &str, body: &str) {
            let p = repo.join(file);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(p, body).unwrap();
            run_git(repo, &["add", file]);
            run_git(repo, &["commit", "-m", "local change"]);
        }

        fn is_ancestor(repo: &Path, a: &str, b: &str) -> bool {
            std::process::Command::new("git")
                .args(["merge-base", "--is-ancestor", a, b])
                .current_dir(repo)
                .status()
                .unwrap()
                .success()
        }

        /// A diverged clone: one local-only commit, one upstream-only commit.
        fn diverged() -> (tempfile::TempDir, PathBuf) {
            let (tmp, _remote, local) = init_repo_pair();
            commit_local(&local, "LOCAL.md", "mine\n");
            push_upstream_change(&tmp.path().join("seed"), &local, "UP.md", "theirs\n");
            (tmp, local)
        }

        fn orchestrator_identity(dir: &Path) {
            std::fs::write(
                dir.join("vct-module.json"),
                r#"{"id":"orchestrator","version":"0.2.100","description":"x"}"#,
            )
            .unwrap();
        }

        fn quiet(_: &str) {}

        fn backup_branches(repo: &Path) -> Vec<String> {
            out(repo, &["branch", "--list", "vco-backup/*", "--format=%(refname:short)"])
                .lines()
                .map(str::to_string)
                .collect()
        }

        /// Merge through the driver: the REAL merge on a diverged clone, a
        /// fake install.py — every phase runs, the tree holds both sides.
        #[tokio::test]
        async fn merge_kind_merges_a_diverged_clone_through_the_pipeline() {
            if git_missing() {
                return;
            }
            let (_tmp, local) = diverged();
            let mut ops = FakeOps::happy();
            ops.real_repo = Some(local.clone());
            let (r, _) = run(&mut ops, UpdateKind::Merge).await;
            let out_ = r.unwrap_or_else(|e| panic!("{}", e.message()));
            assert!(out_.install_py_ran && ops.called("run_install_py"));
            assert!(is_ancestor(&local, "vco_upstream/main", "HEAD"), "upstream merged");
            assert!(local.join("LOCAL.md").is_file() && local.join("UP.md").is_file());
        }

        /// Rebase through the driver: local commits replayed on upstream.
        #[tokio::test]
        async fn rebase_kind_replays_local_commits_through_the_pipeline() {
            if git_missing() {
                return;
            }
            let (_tmp, local) = diverged();
            let mut ops = FakeOps::happy();
            ops.real_repo = Some(local.clone());
            let (r, _) = run(&mut ops, UpdateKind::Rebase).await;
            r.unwrap_or_else(|e| panic!("{}", e.message()));
            assert!(ops.called("run_install_py"));
            assert!(is_ancestor(&local, "vco_upstream/main", "HEAD"));
            assert_eq!(out(&local, &["rev-list", "--count", "vco_upstream/main..HEAD"]), "1");
        }

        /// A merge that conflicts stops at phase 6 as a typed `Conflict`
        /// (the modal's payload), writes the resume sentinel, and never runs
        /// install.py; the driver restores the binaries.
        #[tokio::test]
        async fn a_conflicting_merge_is_a_conflict_and_install_never_runs() {
            if git_missing() {
                return;
            }
            let (tmp, _remote, local) = init_repo_pair();
            commit_local(&local, "vco_lib/foo.py", "def local(): pass\n");
            push_upstream_change(&tmp.path().join("seed"), &local, "vco_lib/foo.py", "def up(): pass\n");
            let mut ops = FakeOps::happy();
            ops.real_repo = Some(local.clone());
            let (r, ledger) = run(&mut ops, UpdateKind::Merge).await;
            let e = r.expect_err("conflict");
            assert_eq!(e.kind(), "Conflict", "{}", e.message());
            let v = e.to_json_value();
            assert_eq!(v["operation"], "merge");
            assert_eq!(v["conflicted_files"][0], "vco_lib/foo.py");
            assert!(!ops.called("run_install_py"));
            assert!(ops.called("abort_restore"));
            assert_eq!(ledger.failed_phase(), Some(Phase::GitOp));
            assert!(crate::commands::installer::read_update_resume_sentinel(&local).is_some());
            let _ = std::process::Command::new("git")
                .args(["merge", "--abort"])
                .current_dir(&local)
                .status();
        }

        /// Resume's verdicts, both arms of each gate: no record → refused;
        /// HEAD never moved + not behind → nothing pending (a no-op
        /// success); HEAD never moved + behind → refused AS stale; HEAD
        /// advanced past the record → proceed.
        #[tokio::test]
        async fn resume_verdicts() {
            if git_missing() {
                return;
            }
            let (tmp, _remote, local) = init_repo_pair();
            assert!(matches!(
                classify_resume(&local).await,
                ResumeVerdict::Refuse { code: "resume_not_pending", .. }
            ));
            let h = head(&local);
            crate::commands::installer::write_update_resume_sentinel(&local, "merge", "main", &h);
            assert!(matches!(
                classify_resume(&local).await,
                ResumeVerdict::NothingPending { .. }
            ));
            push_upstream_change(&tmp.path().join("seed"), &local, "UP.md", "x\n");
            assert!(matches!(
                classify_resume(&local).await,
                ResumeVerdict::Refuse { stale_sentinel: true, .. }
            ));
            commit_local(&local, "LOCAL.md", "resolved\n");
            assert!(matches!(
                classify_resume(&local).await,
                ResumeVerdict::Proceed { .. }
            ));
        }

        /// ResetHard ACT (owner F-W2-03 + W2R-03): the local commit, a
        /// tracked modification and untracked files — one of them at a path
        /// the reset will overwrite — are saved to a `vco-backup/<stamp>`
        /// branch, a `-wip` branch and a verified bundle BEFORE the reset;
        /// the reset lands; the untracked file the reset does not touch is
        /// still on disk.
        #[tokio::test]
        async fn reset_hard_saves_commits_and_the_whole_working_tree_first() {
            if git_missing() {
                return;
            }
            let (tmp, _remote, local) = init_repo_pair();
            orchestrator_identity(&local);
            commit_local(&local, "LOCAL.md", "my commit\n");
            let local_head = head(&local);
            push_upstream_change(&tmp.path().join("seed"), &local, "NEW.md", "upstream\n");
            std::fs::write(local.join("vco_lib/foo.py"), "def edited(): pass\n").unwrap();
            std::fs::write(local.join("NEW.md"), "mine, untracked\n").unwrap();
            std::fs::write(local.join("notes.txt"), "untracked notes\n").unwrap();
            let backups = tmp.path().join("vct_root").join("backups");

            let g = reset_hard_git_op(&local, &backups, &quiet).await.unwrap_or_else(|f| {
                panic!("{}", f.error.message())
            });
            let b = g.reset_backup.expect("backup recorded");
            // The reset happened.
            assert_eq!(head(&local), out(&local, &["rev-parse", "vco_upstream/main"]));
            // The commit is saved (branch points at the pre-reset HEAD).
            assert_eq!(out(&local, &["rev-parse", &b.branch]), local_head);
            assert_eq!(b.local_commits, 1);
            // The working tree is saved: the tracked edit and BOTH untracked
            // files, including the one the reset overwrote.
            let wip = b.uncommitted_branch.clone().expect("dirty tree → wip branch");
            assert_eq!(out(&local, &["show", &format!("{wip}:NEW.md")]), "mine, untracked");
            assert_eq!(out(&local, &["show", &format!("{wip}:vco_lib/foo.py")]), "def edited(): pass");
            assert_eq!(out(&local, &["show", &format!("{wip}:notes.txt")]), "untracked notes");
            // The bundle exists and verifies.
            let bundle = b.bundle.clone().expect("bundle");
            assert!(bundle.starts_with(&backups) && bundle.is_file());
            assert!(std::process::Command::new("git")
                .args(["bundle", "verify", bundle.to_str().unwrap()])
                .current_dir(&local)
                .output()
                .unwrap()
                .status
                .success());
            // The reset left the untracked file it does not own alone.
            assert!(local.join("notes.txt").is_file());
            assert!(b.describe().contains(&b.branch));
        }

        /// ResetHard LEAVE-ALONE: when the backup cannot be written, the reset
        /// is REFUSED — HEAD, the tracked edit and the index are untouched.
        #[tokio::test]
        async fn reset_hard_is_refused_when_the_backup_fails() {
            if git_missing() {
                return;
            }
            let (tmp, _remote, local) = init_repo_pair();
            orchestrator_identity(&local);
            commit_local(&local, "LOCAL.md", "my commit\n");
            let local_head = head(&local);
            push_upstream_change(&tmp.path().join("seed"), &local, "NEW.md", "upstream\n");
            std::fs::write(local.join("vco_lib/foo.py"), "def edited(): pass\n").unwrap();
            // `backups` is a FILE, so the bundle directory cannot be created.
            let backups = tmp.path().join("not_a_dir");
            std::fs::write(&backups, "x").unwrap();

            let f = reset_hard_git_op(&local, &backups, &quiet).await.expect_err("refused");
            assert!(matches!(
                f.error,
                UpdateSurfaceError::Refused { code: "reset_backup_failed", .. }
            ));
            assert!(!f.restored);
            assert_eq!(head(&local), local_head, "nothing was reset");
            assert_eq!(
                std::fs::read_to_string(local.join("vco_lib/foo.py")).unwrap(),
                "def edited(): pass\n"
            );
            // W3R-17: the refusal leaves no half-made backup branch behind.
            assert!(backup_branches(&local).is_empty(), "{:?}", backup_branches(&local));
            match f.error {
                UpdateSurfaceError::Refused { reason, .. } => {
                    assert!(reason.contains("partial backup"), "{}", reason)
                }
                other => panic!("{:?}", other),
            }
        }

        /// ResetHard on a repository that is NOT an orchestrator clone is
        /// refused before the backup and before the reset.
        #[tokio::test]
        async fn reset_hard_refuses_a_non_orchestrator_repo() {
            if git_missing() {
                return;
            }
            let (tmp, _remote, local) = init_repo_pair();
            commit_local(&local, "LOCAL.md", "my commit\n");
            let local_head = head(&local);
            let f = reset_hard_git_op(&local, &tmp.path().join("b"), &quiet)
                .await
                .expect_err("not a clone");
            assert!(matches!(
                f.error,
                UpdateSurfaceError::Refused { code: "reset_target_not_orchestrator", .. }
            ));
            assert_eq!(head(&local), local_head);
            assert!(backup_branches(&local).is_empty(), "no backup branch either");
        }

        /// Nothing local to lose: the HEAD branch is kept, no bundle is made
        /// (git refuses an empty one) and the reset proceeds.
        #[tokio::test]
        async fn reset_hard_with_nothing_local_needs_no_bundle() {
            if git_missing() {
                return;
            }
            let (tmp, _remote, local) = init_repo_pair();
            let b = create_reset_backup(&local, "vco_upstream/main", "main", &tmp.path().join("b"), "T1", &quiet)
                .await
                .expect("backup");
            assert_eq!(b.local_commits, 0);
            assert!(b.bundle.is_none() && b.uncommitted_branch.is_none());
            assert_eq!(backup_branches(&local), vec!["vco-backup/T1".to_string()]);
        }

        // ----- W3R-01 / W3R-02 / W3R-03 (the W3R-FIX lane) -----

        fn git_ok(repo: &Path, args: &[&str]) -> bool {
            std::process::Command::new("git")
                .args(args)
                .current_dir(repo)
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false)
        }

        /// Local and upstream both change README.md; `op` ("merge" or
        /// "rebase") is started and stops on the conflict; the user then
        /// hand-edits the conflicted file.
        fn stalled(op: &str) -> (tempfile::TempDir, PathBuf) {
            let (tmp, _remote, local) = init_repo_pair();
            orchestrator_identity(&local);
            commit_local(&local, "README.md", "LOCAL\n");
            push_upstream_change(&tmp.path().join("seed"), &local, "README.md", "UPSTREAM\n");
            assert!(!git_ok(&local, &[op, "vco_upstream/main"]), "the {op} must conflict");
            assert!(
                crate::commands::git_user_editable_merge::merge_or_rebase_in_progress(&local)
                    .is_some()
            );
            std::fs::write(local.join("README.md"), "hand-resolved\n").unwrap();
            (tmp, local)
        }

        fn wip_of(repo: &Path) -> Option<String> {
            backup_branches(repo).into_iter().find(|b| b.ends_with("-wip"))
        }

        /// W3R-01 ACT: the abort FAILS on a mid-rebase tree → the reset is
        /// refused; HEAD, `refs/heads/main`, `rebase-merge/` and the hand
        /// edit are untouched, and the saved work is named.
        #[tokio::test]
        async fn reset_hard_is_refused_when_the_abort_fails() {
            if git_missing() {
                return;
            }
            let (tmp, local) = stalled("rebase");
            let head_before = head(&local);
            let main_before = out(&local, &["rev-parse", "refs/heads/main"]);
            let f = reset_hard_git_op_with(
                &local,
                &tmp.path().join("backups"),
                &quiet,
                |_p: PathBuf| async { Err("index.lock exists".to_string()) },
            )
            .await
            .expect_err("refused");
            let reason = match &f.error {
                UpdateSurfaceError::Refused { code: "reset_abort_failed", reason } => reason.clone(),
                other => panic!("{:?}", other),
            };
            assert!(!f.restored);
            assert!(reason.contains("Nothing was reset") && reason.contains("vco-backup/"), "{reason}");
            assert_eq!(head(&local), head_before, "HEAD untouched");
            assert_eq!(out(&local, &["rev-parse", "refs/heads/main"]), main_before);
            assert!(
                local.join(".git").join("rebase-merge").exists()
                    || local.join(".git").join("rebase-apply").exists(),
                "the rebase state is untouched"
            );
            assert_eq!(std::fs::read_to_string(local.join("README.md")).unwrap(), "hand-resolved\n");
            let _ = git_ok(&local, &["rebase", "--abort"]);
        }

        /// W3R-01 ACT: the abort reports success but leaves the merge
        /// state behind → refused as `reset_state_not_clear`.
        #[tokio::test]
        async fn reset_hard_is_refused_when_the_abort_leaves_the_state_behind() {
            if git_missing() {
                return;
            }
            let (tmp, local) = stalled("merge");
            let head_before = head(&local);
            let f = reset_hard_git_op_with(
                &local,
                &tmp.path().join("backups"),
                &quiet,
                |_p: PathBuf| async { Ok(()) },
            )
            .await
            .expect_err("refused");
            assert!(
                matches!(f.error, UpdateSurfaceError::Refused { code: "reset_state_not_clear", .. }),
                "{:?}",
                f.error
            );
            assert_eq!(head(&local), head_before);
            assert!(local.join(".git").join("MERGE_HEAD").exists());
            let _ = git_ok(&local, &["merge", "--abort"]);
        }

        /// W3R-01 ORDER + LEAVE-ALONE: when the abort runs, the backup
        /// already exists AND verifies, and its `-wip` holds the hand edit
        /// the abort is about to discard; the real abort then succeeds and
        /// the reset proceeds to upstream, attached to main.
        #[tokio::test]
        async fn reset_hard_backs_up_before_the_abort_then_proceeds() {
            if git_missing() {
                return;
            }
            let (tmp, local) = stalled("merge");
            let backups = tmp.path().join("backups");
            let seen: std::sync::Arc<std::sync::Mutex<Vec<String>>> = Default::default();
            let rec = seen.clone();
            let bdir = backups.clone();
            let g = reset_hard_git_op_with(&local, &backups, &quiet, move |p: PathBuf| async move {
                let wip = wip_of(&p).expect("wip branch exists before the abort");
                assert_eq!(out(&p, &["show", &format!("{wip}:README.md")]), "hand-resolved");
                let bundle = std::fs::read_dir(&bdir)
                    .expect("backups dir")
                    .flatten()
                    .map(|e| e.path())
                    .find(|p| p.extension().is_some_and(|x| x == "bundle"))
                    .expect("bundle written before the abort");
                assert!(git_ok(&p, &["bundle", "verify", bundle.to_str().unwrap()]));
                rec.lock().unwrap().push("abort".into());
                crate::commands::installer::abort_merge_or_rebase_unclaimed(&p).await
            })
            .await
            .unwrap_or_else(|f| panic!("{}", f.error.message()));
            assert_eq!(*seen.lock().unwrap(), vec!["abort".to_string()], "abort ran once");
            assert!(!local.join(".git").join("MERGE_HEAD").exists());
            assert_eq!(head(&local), out(&local, &["rev-parse", "vco_upstream/main"]));
            assert_eq!(out(&local, &["rev-parse", "--abbrev-ref", "HEAD"]), "main");
            let b = g.reset_backup.expect("backup");
            assert_eq!(b.local_commits, 1);
            let wip = b.uncommitted_branch.expect("wip");
            assert_eq!(out(&local, &["show", &format!("{wip}:README.md")]), "hand-resolved");
        }

        /// W3R-02 ACT: from a DETACHED HEAD (holding its own commit, while
        /// `main` holds a different local commit), ResetHard leaves the clone
        /// on `main`, attached, at upstream; both local commits are saved;
        /// and the pipeline's own PullFf command then advances `main`.
        #[tokio::test]
        async fn reset_hard_from_a_detached_head_lands_attached_on_the_update_branch() {
            if git_missing() {
                return;
            }
            let (tmp, _remote, local) = init_repo_pair();
            orchestrator_identity(&local);
            commit_local(&local, "LOCAL.md", "on main\n");
            let main_tip = head(&local);
            run_git(&local, &["checkout", "--quiet", "--detach", "HEAD~1"]);
            commit_local(&local, "DETACHED.md", "on a detached HEAD\n");
            let detached_tip = head(&local);
            let seed = tmp.path().join("seed");
            push_upstream_change(&seed, &local, "UP.md", "upstream\n");
            assert_eq!(out(&local, &["rev-parse", "--abbrev-ref", "HEAD"]), "HEAD");

            let g = reset_hard_git_op(&local, &tmp.path().join("backups"), &quiet)
                .await
                .unwrap_or_else(|f| panic!("{}", f.error.message()));
            assert_eq!(g.branch, "main");
            let upstream = out(&local, &["rev-parse", "vco_upstream/main"]);
            assert_eq!(out(&local, &["rev-parse", "--abbrev-ref", "HEAD"]), "main", "attached");
            assert_eq!(out(&local, &["rev-parse", "refs/heads/main"]), upstream);
            assert_eq!(head(&local), upstream);
            let b = g.reset_backup.expect("backup");
            assert_eq!(out(&local, &["rev-parse", &b.branch]), detached_tip);
            let tip = b.branch_tip.clone().expect("main's own commit is saved");
            assert_eq!(out(&local, &["rev-parse", &tip]), main_tip);
            assert_eq!(b.local_commits, 2);
            assert!(b.describe().contains(&tip));

            // The next PullFf (the pipeline's exact argv) advances `main`.
            push_upstream_change(&seed, &local, "UP2.md", "later\n");
            let args = crate::commands::git_user_editable_merge::PullPlan::FfOnly
                .pull_args(crate::commands::self_update::VCO_UPSTREAM_REMOTE, "main");
            let args: Vec<&str> = args.iter().map(String::as_str).collect();
            assert!(git_ok(&local, &args), "PullFf after the reset");
            let later = out(&local, &["rev-parse", "vco_upstream/main"]);
            assert_eq!(out(&local, &["rev-parse", "refs/heads/main"]), later, "the branch advanced");
            assert_eq!(out(&local, &["rev-parse", "--abbrev-ref", "HEAD"]), "main");
        }

        /// A clone whose HEAD tracks the two Windows binaries (nothing local).
        fn with_tracked_binaries() -> (tempfile::TempDir, PathBuf) {
            let (tmp, _remote, local) = init_repo_pair();
            let seed = tmp.path().join("seed");
            push_upstream_change(&seed, &local, "launcher/dist/windows-x64/vct-launcher.exe", "L\n");
            push_upstream_change(&seed, &local, "launcher/dist/windows-x64/vct-hub.exe", "H\n");
            run_git(&local, &["merge", "--ff-only", "vco_upstream/main"]);
            (tmp, local)
        }

        /// Phase 5's renames, by hand: canonical → `<binary>.old-<pid>`.
        fn rename_aside(local: &Path) {
            let d = local.join("launcher/dist/windows-x64");
            for b in ["vct-launcher.exe", "vct-hub.exe"] {
                std::fs::rename(d.join(b), d.join(format!("{b}.old-4242"))).unwrap();
            }
        }

        /// W3R-03 ACT: a tree whose only change is the launcher's own rename
        /// artefacts has nothing to lose — no `-wip`, no bundle.
        #[tokio::test]
        async fn rename_artefacts_alone_are_nothing_to_lose() {
            if git_missing() {
                return;
            }
            let (tmp, local) = with_tracked_binaries();
            rename_aside(&local);
            // A stale artefact from an earlier run (canonical present).
            std::fs::write(local.join("launcher/dist/windows-x64/vct-launcher.exe.old-17"), "x").unwrap();
            let b = create_reset_backup(&local, "vco_upstream/main", "main", &tmp.path().join("b"), "T2", &quiet)
                .await
                .expect("backup");
            assert_eq!(b.uncommitted_branch, None, "no -wip for artefacts only");
            assert_eq!(b.bundle, None);
            assert!(b.describe().contains("no local commits or uncommitted changes"));
            assert_eq!(backup_branches(&local), vec!["vco-backup/T2".to_string()]);
        }

        /// W3R-03 LEAVE-ALONE: real user files — a note, and a hand-made
        /// `vct-launcher.old-may7` copy (not the `.old-<pid>` shape) — ARE
        /// captured in `-wip` and in the bundle; the artefacts are not, and
        /// the renamed-away binary is kept at HEAD's copy, not deleted.
        #[tokio::test]
        async fn user_files_are_captured_next_to_excluded_artefacts() {
            if git_missing() {
                return;
            }
            let (tmp, local) = with_tracked_binaries();
            rename_aside(&local);
            std::fs::write(local.join("notes.txt"), "mine\n").unwrap();
            std::fs::write(local.join("launcher/dist/windows-x64/vct-launcher.old-may7"), "copy\n").unwrap();
            let bdir = tmp.path().join("b");
            let b = create_reset_backup(&local, "vco_upstream/main", "main", &bdir, "T3", &quiet)
                .await
                .expect("backup");
            let wip = b.uncommitted_branch.clone().expect("user files → wip");
            let files = out(&local, &["ls-tree", "-r", "--name-only", &wip]);
            assert!(files.lines().any(|l| l == "notes.txt"), "{files}");
            assert!(files.lines().any(|l| l == "launcher/dist/windows-x64/vct-launcher.old-may7"));
            assert!(!files.contains(".old-4242"), "artefacts excluded: {files}");
            assert!(files.lines().any(|l| l == "launcher/dist/windows-x64/vct-launcher.exe"));
            assert!(files.lines().any(|l| l == "launcher/dist/windows-x64/vct-hub.exe"));

            // The bundle carries it: fetch it into a fresh repo that has upstream.
            let bundle = b.bundle.expect("bundle");
            let fresh = tmp.path().join("fresh");
            std::fs::create_dir_all(&fresh).unwrap();
            run_git(&fresh, &["init", "--quiet"]);
            let remote = tmp.path().join("remote.git");
            run_git(&fresh, &["fetch", "--quiet", remote.to_str().unwrap(), "main:refs/remotes/up/main"]);
            run_git(&fresh, &["fetch", "--quiet", bundle.to_str().unwrap(), "refs/heads/*:refs/remotes/b/*"]);
            assert_eq!(out(&fresh, &["show", "b/vco-backup/T3-wip:notes.txt"]), "mine");
        }
    }

    // ----- F-W3-14: resolvers act only on the resolved root -----

    fn git(dir: &Path, args: &[&str]) -> String {
        let o = std::process::Command::new("git").args(args).current_dir(dir).output().unwrap();
        String::from_utf8_lossy(&o.stdout).to_string()
    }

    /// ACT: a GUI path that is not the resolved root is refused with the
    /// typed Refused error, and the tree it names is left exactly as it was.
    #[test]
    fn a_resolver_path_that_is_not_the_resolved_root_is_refused_and_nothing_changes() {
        let other = tempfile::tempdir().unwrap();
        let root = tempfile::tempdir().unwrap();
        git(other.path(), &["init", "-q"]);
        std::fs::write(other.path().join("f.txt"), "a\n").unwrap();
        git(other.path(), &["add", "f.txt"]);
        git(other.path(), &["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"]);
        let head = git(other.path(), &["rev-parse", "HEAD"]);
        let err = check_gui_path(other.path(), root.path()).unwrap_err();
        assert_eq!(err.kind(), "Refused");
        assert!(err.to_json().contains("path_not_install_root"), "{}", err.to_json());
        assert_eq!(git(other.path(), &["rev-parse", "HEAD"]), head, "HEAD untouched");
        assert_eq!(git(other.path(), &["status", "--porcelain"]), "", "tree untouched");
    }

    /// LEAVE-ALONE: the same directory (also through a `..` detour or a
    /// symlink) proceeds.
    #[test]
    fn a_resolver_path_equal_to_the_resolved_root_proceeds() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("sub")).unwrap();
        assert!(check_gui_path(root.path(), root.path()).is_ok());
        assert!(check_gui_path(&root.path().join("sub").join(".."), root.path()).is_ok());
        #[cfg(unix)]
        {
            let link = tempfile::tempdir().unwrap();
            let l = link.path().join("root-link");
            std::os::unix::fs::symlink(root.path(), &l).unwrap();
            assert!(check_gui_path(&l, root.path()).is_ok());
        }
        assert!(!gui_path_matches_root(Path::new("/nonexistent/vco"), root.path()));
    }

    /// No database → refused (never "assume the GUI path").
    #[test]
    fn no_database_refuses() {
        let err = require_gui_path_is_resolved_root(None, "/tmp").unwrap_err();
        assert!(err.contains("no_database"), "{err}");
    }
}
