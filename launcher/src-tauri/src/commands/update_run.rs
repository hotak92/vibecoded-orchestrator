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
//! | 6 | `git_op` | by kind: fast-forward pull / hard reset / none |
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
//! Kinds `Merge`, `Rebase` and `Resume` are part of the enum (the contract
//! WP-08 builds against) but their git operation still lives inline in
//! `installer.rs`; until WP-03b extracts it they are REFUSED in preflight —
//! before any mutation — with code `kind_not_routed`. See
//! [`kind_is_routed`].

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
    /// Merge upstream into a diverged clone (WP-03b routes it).
    Merge,
    /// Rebase a diverged clone onto upstream (WP-03b routes it).
    Rebase,
    /// Continue an update halted at a resolved conflict (WP-03b routes it).
    Resume,
    /// No git operation: re-apply `install.py --update` to the current tree.
    ApplyOnly,
    /// `git reset --hard vco_upstream/<branch>` — the diverged-clone rescue.
    ResetHard,
}

/// The command argument's type name in the contract.
pub type UpdateKindDto = UpdateKind;

/// Whether `run_update` can perform `kind` today. `Merge`/`Rebase`/`Resume`
/// are refused in preflight (no mutation) until WP-03b moves their git
/// operation out of `installer.rs` into [`UpdateOps::git_op`].
pub(crate) fn kind_is_routed(kind: UpdateKind) -> bool {
    matches!(
        kind,
        UpdateKind::PullFf | UpdateKind::ApplyOnly | UpdateKind::ResetHard
    )
}

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
    /// The dist is below the source but newer than the running launcher —
    /// relaunching into it is strictly better (deferral records the gap).
    Partial {
        running: String,
        dist: String,
        source: String,
    },
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

/// Pure decision for phase 11. `caught_up` is the existing
/// `installer::WaitForBinaryRefresh` answer ("launcher and hub sidecars are
/// at or above source"), asked ONCE with its re-pull disabled.
pub(crate) fn decide_binary_refresh(
    running: &str,
    source: Option<&str>,
    caught_up: bool,
    dist: Option<&str>,
) -> BinaryCheck {
    use vct_launcher_core::version;
    let Some(source) = source else {
        return BinaryCheck::Unknown {
            reason: "the source version (vct-module.json) could not be read".into(),
        };
    };
    match version::is_older(running, source) {
        Err(e) => {
            return BinaryCheck::Unknown {
                reason: e.to_string(),
            }
        }
        Ok(false) => return BinaryCheck::RunningCurrent,
        Ok(true) => {}
    }
    if caught_up {
        return BinaryCheck::Ready;
    }
    let dist_s = dist.unwrap_or("").to_string();
    match dist.map(|d| version::is_older(running, d)) {
        Some(Ok(true)) => BinaryCheck::Partial {
            running: running.into(),
            dist: dist_s,
            source: source.into(),
        },
        Some(Err(e)) => BinaryCheck::Unknown {
            reason: e.to_string(),
        },
        _ => BinaryCheck::NotPublished {
            running: running.into(),
            dist: if dist_s.is_empty() {
                "<unknown>".into()
            } else {
                dist_s
            },
            source: source.into(),
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

    let message = compose_message(
        if restarted {
            "Orchestrator updated; relaunching into the new launcher binary."
        } else {
            "Orchestrator updated."
        },
        binary_note.as_deref(),
        relaunch_note.as_deref(),
    );
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
    if !kind_is_routed(kind) {
        return Err(UpdateSurfaceError::Refused {
            code: "kind_not_routed",
            reason: format!(
                "The {:?} update is not available through {} yet — nothing was changed. Use \
                 the Merge / Rebase / Continue buttons of the update modal.",
                kind, SURFACE
            ),
        });
    }
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
    fn new(app: AppHandle<R>, window: Option<&'w Window>) -> Self {
        Self {
            app,
            window,
            flight: None,
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
        let flight = crate::commands::single_flight::begin_orchestrator_update_or_refuse()
            .map_err(|reason| UpdateSurfaceError::Refused {
                code: "already_running",
                reason,
            })?;
        let lock = crate::commands::single_flight::acquire_update_lock().map_err(|reason| {
            UpdateSurfaceError::Refused {
                code: "already_running",
                reason,
            }
        })?;
        self.flight = Some(flight);
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
            match crate::commands::update_pipeline::run_preflight_refusals(
                &root,
                SURFACE,
                &crate::commands::update_pipeline::ExtraPreflight::None,
            )
            .await
            {
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
        let before = match kind {
            UpdateKind::ResetHard => Some("git reset --hard"),
            UpdateKind::ApplyOnly => Some("install.py --update"),
            _ => Some("git pull"),
        };
        let window = self.window;
        crate::commands::update_pipeline::stop_hub_and_rename_binaries_aside(
            root,
            "update",
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
                UpdateKind::ResetHard => reset_hard_git_op(&root).await,
                // Refused in preflight (`kind_is_routed`) / handled by the
                // driver (ApplyOnly) — reaching here is a driver bug, worded.
                other => Err(GitOpFailure {
                    error: UpdateSurfaceError::Raw(format!(
                        "internal: {:?} reached the git operation phase unrouted",
                        other
                    )),
                    restored: false,
                }),
            }
        }
    }

    fn head_advance(&mut self, root: &Path) -> impl Future<Output = Result<(), String>> + Send {
        let root = root.to_path_buf();
        async move {
            crate::commands::installer::assert_head_reached_upstream(&root)
                .await
                .map(|_| ())
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
            // The existing "sidecars caught up with source" rule, asked ONCE:
            // zero timeout, re-pull disabled (L2-F11 — no git here).
            let caught_up = crate::commands::installer::WaitForBinaryRefresh {
                install_path: &root,
                branch: &branch,
                timeout: std::time::Duration::ZERO,
                interval: std::time::Duration::ZERO,
                disable_git_pull: true,
            }
            .run()
            .await
            .is_ok();
            let source = crate::commands::installer::read_source_version(&root);
            let dist = crate::commands::installer::read_on_disk_binary_version(&root);
            let check = decide_binary_refresh(
                RUNNING_VERSION,
                source.as_deref(),
                caught_up,
                dist.as_deref(),
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
        extra_preflight: crate::commands::update_pipeline::ExtraPreflight::None,
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

/// `ResetHard`'s git operation: identity re-asserted, fetch, abort any
/// in-progress merge/rebase (a reset does not clear them), then
/// `git reset --hard vco_upstream/<branch>`. Failures leave the restore to
/// the driver (`restored: false`).
async fn reset_hard_git_op(root: &Path) -> Result<GitOpOutcome, GitOpFailure> {
    let fail = |error: UpdateSurfaceError| GitOpFailure {
        error,
        restored: false,
    };
    if let Some(refusal) = reset_hard_identity_refusal(root) {
        return Err(fail(refusal));
    }
    crate::commands::update_pipeline::clear_prior_resume_state(root);
    let branch = crate::commands::git_cmd::resolve_branch(root)
        .await
        .map_err(|e| {
            fail(UpdateSurfaceError::Raw(format!(
                "git rev-parse failed: {}",
                e
            )))
        })?
        .name;
    crate::commands::self_update::serialized_fetch_upstream(
        root,
        crate::commands::self_update::FetchPolicy::Quick,
        Some(&branch),
    )
    .await
    .map_err(|e| {
        fail(UpdateSurfaceError::Raw(format!(
            "fetching upstream failed: {}",
            e
        )))
    })?;
    if let Err(e) = crate::commands::installer::abort_merge_or_rebase_unclaimed(root).await {
        tracing::warn!(
            "[vct] {}: could not abort the in-progress merge/rebase before the reset ({}) — \
             resetting anyway",
            SURFACE,
            e
        );
    }
    let target = format!(
        "{}/{}",
        crate::commands::self_update::VCO_UPSTREAM_REMOTE,
        branch
    );
    let out = crate::commands::git_cmd::run_git_raw(root, &["reset", "--hard", target.as_str()])
        .await
        .map_err(|e| fail(UpdateSurfaceError::Raw(e)))?;
    if !out.status.success() {
        return Err(fail(UpdateSurfaceError::Raw(format!(
            "git reset --hard {} failed: {}",
            target,
            String::from_utf8_lossy(&out.stderr).trim()
        ))));
    }
    Ok(GitOpOutcome {
        already_up_to_date: false,
        dist_binary_stale: false,
        branch,
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
    let mut ops = LiveOps::new(app, window);
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
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
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
                }),
                head: Ok(()),
                install: Ok(ok_run()),
                binary: BinaryCheck::Ready,
                relaunch: Ok(RelaunchOutcome::Spawned {
                    exe: PathBuf::from("/fake/root/launcher/dist/x/vct-launcher"),
                }),
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
            _kind: UpdateKind,
            _renames: &PrePullRenames,
            _head_before: Option<String>,
        ) -> impl Future<Output = Result<GitOpOutcome, GitOpFailure>> + Send {
            self.rec("git_op");
            let v = self.git_op.clone();
            async move { v }
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
    /// tree, an unreadable hub.pid, an unrouted kind, and (last, being the one
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
        cases.push(("merge kind not routed", FakeOps::happy(), UpdateKind::Merge));
        cases.push((
            "rebase kind not routed",
            FakeOps::happy(),
            UpdateKind::Rebase,
        ));
        cases.push((
            "resume kind not routed",
            FakeOps::happy(),
            UpdateKind::Resume,
        ));
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

    /// Phase 11's pure decision, both directions (AD-8 tri-state).
    #[test]
    fn decide_binary_refresh_table() {
        assert_eq!(
            decide_binary_refresh("0.2.100", Some("0.2.100"), false, None),
            BinaryCheck::RunningCurrent
        );
        assert_eq!(
            decide_binary_refresh("0.2.99", Some("0.2.100"), true, Some("0.2.100")),
            BinaryCheck::Ready
        );
        assert!(matches!(
            decide_binary_refresh("0.2.98", Some("0.2.100"), false, Some("0.2.99")),
            BinaryCheck::Partial { .. }
        ));
        assert!(matches!(
            decide_binary_refresh("0.2.99", Some("0.2.100"), false, Some("0.2.99")),
            BinaryCheck::NotPublished { .. }
        ));
        assert!(matches!(
            decide_binary_refresh("0.2.99", Some("0.2.100"), false, None),
            BinaryCheck::NotPublished { .. }
        ));
        assert!(matches!(
            decide_binary_refresh("0.2.99", None, false, None),
            BinaryCheck::Unknown { .. }
        ));
        assert!(matches!(
            decide_binary_refresh("0.2.99", Some("0.2.100-rc1"), false, None),
            BinaryCheck::Unknown { .. }
        ));
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

    /// Legacy update commands that do NOT yet reach `run_update`. WP-03b
    /// migrates each to delegate (moving it to the list above) or removes it
    /// (`update_orchestrator_at`, owner Q1). Shrinking this list is the point;
    /// a name that starts reaching `run_update` must move up.
    const PENDING_WP03B: [&str; 8] = [
        "update_orchestrator",
        "merge_orchestrator_with_upstream",
        "rebase_orchestrator_onto_upstream",
        "resume_orchestrator_update",
        "apply_launcher_update",
        "apply_pending_install",
        "force_resync_launcher",
        "update_orchestrator_at",
    ];

    const SCANNED: [&str; 3] = [
        "src/commands/update_run.rs",
        "src/commands/installer.rs",
        "src/commands/self_update.rs",
    ];

    /// Blank out comments and string/char literal CONTENTS (keeping offsets),
    /// so a name in a comment or a string cannot satisfy the scan.
    fn code_only(src: &str) -> String {
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

    /// Every allowlisted `#[command]` reaches `run_update(` in its own CODE
    /// (comments and strings blanked), is registered in `generate_handler!`,
    /// and every pending legacy name still exists and does NOT yet reach it
    /// (a migrated one must move to the allowlist). Red-proof: delete the
    /// `run_update(` call from `run_orchestrator_update` → red.
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
        let lib = code_only(
            &std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lib.rs"))
                .unwrap(),
        );
        assert!(
            lib.contains("commands::update_run::run_orchestrator_update"),
            "run_orchestrator_update must be registered in generate_handler!"
        );
        for name in PENDING_WP03B {
            let body = find_body(&files, name).unwrap_or_else(|| {
                panic!("pending #[command] {name} is gone — remove it from PENDING_WP03B")
            });
            assert!(
                !body.contains("run_update("),
                "{name} now reaches run_update — move it to REACHES_RUN_UPDATE"
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
}
