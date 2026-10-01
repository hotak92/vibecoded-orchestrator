// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! The orchestrator-repo update pipeline's building blocks: everything
//! between "a user asked for an update" and "the tree is at the upstream tip",
//! plus `install.py --update`.
//!
//! HISTORY. Two commands used to update the SAME git clone — the MenuBar
//! badge's and the Preferences page's — and each was its own program over
//! shared subroutines; they drifted in twelve places (see
//! `.claude/context/reviews/UPDATE-SURFACES-DUPLICATION-2026-09-18.md` §2).
//! v0.2.95 moved the shared SEQUENCE here; v0.2.100 (AD-1) went the rest of
//! the way: `update_run::run_update` is the ONE pipeline, composing the
//! phase-level pieces below under its phase ledger, and every per-surface
//! command it replaced — with its own tail, error renderer and restart — is
//! gone (WP-03b).
//!
//! WHAT LIVES HERE: the in-progress refusal ([`run_preflight_refusals`]), the
//! hub stop + binary renames ([`stop_hub_and_rename_binaries_aside`]), the
//! git operation of each kind — [`pull_to_upstream`] (`PullFf`),
//! [`merge_upstream`], [`rebase_onto_upstream`], [`classify_resume`] — and the
//! `install.py --update` runner ([`run_install_py_update`]).
//!
//! ORDERING IS LOAD-BEARING. Comments inside the moved code say why an
//! adjacent pair must stay in its order (the in-progress refusal before the
//! sentinel clear; the pre-pull rename before F1 and the reconcile; the
//! generated-reconcile deferral after the pull). Nothing here may be
//! reordered for tidiness.
//!
//! WHAT THE CALLER OWNS: the claim, the audit-row WRITES (this module only
//! collects them), error RENDERING (`update_failure`, one home), the restore
//! on failure, and the relaunch.
//!
//! `stop_hub` / `run_pre_merge_a0` toggles do not exist on purpose: the clone's
//! `launcher/dist/*/vct-hub*` are TRACKED files, so no caller provably owns no
//! hub, and a `false` arm would be unread configuration whose only effect
//! could be to switch OFF a guard over a destructive act. Do not add them.

use std::path::{Path, PathBuf};

use tauri::Window;

use crate::commands::git_user_editable_merge::{
    write_launcher_update_diverged_deferral, LauncherUpdateDivergedKind,
};
use crate::commands::installer::{
    abort_update_restore_binaries_and_hub, assert_head_reached_upstream,
    clear_update_resume_deferral_if_solo, clear_update_resume_sentinel, collect_conflicted_files,
    collect_diverged_files, emit_progress, ensure_hub_stopped_for_update,
    handle_install_phase_exit, handle_merge_in_progress_refusal,
    handle_untracked_overwrite_post_pull, installer_step_to_user_label, is_merge_or_rebase_conflict,
    log_conflict_payload_return, log_generic_pull_failure, maybe_emit_pre_merge_deferrals,
    pre_pull_rename_vct_hub_binary, read_head_sha, read_remote_sha,
    record_install_spawn_failure, refuse_if_merge_or_rebase_in_progress,
    run_pre_merge_user_editable, write_autostash_pop_conflict_deferral,
    write_resume_sentinel_and_deferral, write_update_resume_sentinel, DivergedFiles,
    HeadAdvanceOutcome,
};
use crate::commands::update_gate::UpdateInProgressGuard;
use crate::services::binary_freshness::pre_pull_rename_running_binary;


/// What the calling surface has to tell the pipeline about ITSELF.
///
/// Everything here is context the pipeline cannot derive: which command is
/// running (it appears verbatim in log lines and in
/// `refuse_if_merge_or_rebase_in_progress`'s `surface` argument), where to send
/// progress, and the values the audit rows carry that were captured BEFORE the
/// pipeline started.
pub(crate) struct UpdatePipelineOptions<'a> {
    /// The calling command's name, as it appears in this project's log lines,
    /// in `refuse_if_merge_or_rebase_in_progress`'s `surface` argument, and as
    /// the PREFIX of the audit-row operation names this pipeline collects
    /// (`<surface>_complete`, `<surface>_post_pull_unverified`) — which is what
    /// they already are for the installer surface.
    pub surface: &'static str,
    /// Where to emit `install_progress` events. `None` for a surface that has
    /// no progress modal.
    pub emit_progress_to: Option<&'a Window>,
    /// The install path EXACTLY as the caller received it — the audit rows
    /// carry this string, not a re-rendered `Path`.
    pub install_path_label: &'a str,
    /// The branch resolved BEFORE the pipeline ran (the `_start` audit row's
    /// value). Deliberately NOT the same value as the pull branch: this one
    /// falls back to `FALLBACK_BRANCH`, the pull branch hard-errors.
    pub start_branch: &'a str,
    /// `HEAD` before the pull. The autostash-pop sentinel needs a SHA that is
    /// guaranteed different from the now-advanced HEAD, so resume's
    /// HEAD-advance guard passes.
    pub head_sha_before: Option<String>,
    /// Start of the update, for the `duration_ms` field of the audit rows the
    /// pipeline collects.
    pub update_start_ms: i64,
}


/// Why the pipeline stopped, in terms of the CONDITION rather than of any one
/// surface's payload string.
///
/// ONE rendering (v0.2.100 WP-03b): `update_failure::from_pipeline_error`
/// turns it into the `{kind, message, …}` contract whose payload kinds keep
/// the `orchestrator_update_*` event names the decision modals parse. The
/// per-surface renderings (`installer`'s events, `self_update`'s resync
/// payload) went with their commands.
///
/// The two payload-carrying variants are the ones whose payload is produced by
/// a SHARED handler that also writes a sentinel (`handle_*`), so splitting the
/// string out would split the write from the thing it describes.
pub(crate) enum UpdatePipelineError {
    /// A merge or rebase was already in progress — before the pull (the
    /// pre-flight) or reported by the pull itself. `payload` is what the
    /// shared handler produced, alongside the sentinel it wrote.
    MergeInProgress { payload: String },
    /// The pull conflicted.
    ///
    /// `record_binary_clobber_averted` asks the caller for its
    /// `update_binary_clobber_averted` audit row: the abort tail declined to
    /// rename the old binary back over freshly-pulled bytes (WI-3). It is
    /// false on the PRE-pull classification path — not because no clobber can
    /// be averted there, but because that path discards the abort tail's
    /// outcome today and phase 1 adds no row that does not exist.
    Conflict {
        operation: &'static str,
        branch: String,
        conflicted: Vec<String>,
        detail: String,
        record_binary_clobber_averted: bool,
    },
    /// The merge SUCCEEDED and only the `--autostash` POP of the local WIP
    /// conflicted. A distinct condition, not a failed merge.
    AutostashPopConflict {
        branch: String,
        conflicted: Vec<String>,
        detail: String,
        record_binary_clobber_averted: bool,
    },
    /// Git aborted BEFORE merging because an untracked local file sits at a
    /// path upstream is about to add. `payload` comes from the shared handler
    /// that resolved the colliding paths out of git's stderr.
    UntrackedCollision { payload: String },
    /// The local clone has diverged from upstream. The three file sets are the
    /// ones the divergence modal renders.
    NonFastForward {
        branch: String,
        local_sha: Option<String>,
        remote_sha: Option<String>,
        diverged: Vec<String>,
        upstream_only: Vec<String>,
        local_only: Vec<String>,
        detail: String,
    },
    /// The pull exited 0 but HEAD did not reach the upstream tip. Running
    /// `install.py` on that tree is the v0.2.62 GUI-update crash class.
    HeadDidNotAdvance { detail: String },
    /// Anything else, already worded for the user.
    Raw(String),
}

/// The two binaries `git` is about to write over, renamed aside (Windows) so
/// its atomic rename cannot hit `ERROR_SHARING_VIOLATION`. Both are `None` on
/// POSIX and on a tree whose binaries live outside the install root.
///
/// A named struct rather than a tuple ON PURPOSE: both fields are
/// `Option<PathBuf>`, so a swapped tuple would compile silently and hand the
/// abort tail the WRONG path to restore — reverting the launcher binary over
/// the hub's, which is the clobber class v0.2.91 WI-3 exists to prevent.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub(crate) struct PrePullRenames {
    pub hub: Option<PathBuf>,
    pub launcher: Option<PathBuf>,
}

/// Capitalise the first character — the abort message says "Update aborted",
/// the progress line says "...for update...", and BOTH are derived from the one
/// `operation` word so the two can never name different operations.
fn capitalise_first(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        Some(c) => c.to_uppercase().collect::<String>() + chars.as_str(),
        None => String::new(),
    }
}

/// THE gate: may the caller write the tree, given what the hub stop returned?
///
/// Pure, so BOTH arms of a branch that gates a destructive act are unit-
/// testable — the house rule (cf. `installer::tracked_gate_refuses`, whose doc
/// gives the same reason). The act it gates is whichever tree-write follows:
/// a `git pull`, a `git merge`/`rebase`, an `install.py --update`, or the
/// `ResetHard` kind's `git reset --hard`.
///
/// `Ok(_)` from the stop — `true` "a hub was stopped", `false` "none was
/// running" — both mean the same thing here: nothing holds the binaries.
/// `Err` means a hub is provably still alive, and proceeding would either
/// abort the git operation atomically (Windows) or leave the running hub
/// serving old code from a deleted inode (POSIX). Refuse.
///
/// `before` names the act in the refusal ("could not stop vct-hub before git
/// pull"); `None` omits the clause, which is what the resume surface wants —
/// it runs no git command at all, its next step is install.py.
pub(crate) fn gate_destructive_write_on_hub_stop(
    stopped: Result<bool, String>,
    operation: &str,
    before: Option<&str>,
) -> Result<(), String> {
    let Err(e) = stopped else {
        return Ok(());
    };
    let before = match before {
        Some(what) => format!(" before {}", what),
        None => String::new(),
    };
    Err(format!(
        "{} aborted: could not stop vct-hub{}: {}. \
         Try again, or run `vct-hub --stop` manually.",
        capitalise_first(operation),
        before,
        e
    ))
}

/// Stage 0a/0b/0c: stop the detached `vct-hub`, then rename the hub and
/// launcher binaries aside — everything that must happen before ANY process
/// writes the install tree.
///
/// ONE HOME (v0.2.95 phase 3). This trio was written out FOUR times: the
/// pipeline, `installer::merge_orchestrator_with_upstream`,
/// `installer::rebase_orchestrator_onto_upstream` and
/// `installer::resume_orchestrator_update` — and, as the gap this closes, NOT
/// at all in `self_update::force_resync_launcher`, whose `git reset --hard`
/// writes the same tracked binaries. Each copy worded its own refusal and its
/// own progress line, so the four drifted in wording while agreeing in
/// substance; the fifth (resync) was simply missing. Adding a fifth copy is
/// the case the project's modularity rule forbids outright, so all four were
/// migrated onto this BEFORE the resync call site was added. Since v0.2.100
/// its one caller is `update_run`'s phase 5, for every kind.
///
/// WHY it protects what it protects (`07101d30`, v0.2.21 Step 12, Reviewer B
/// blocker B1):
///   1. `launcher/dist/<arch>/vct-hub{,.exe}` and `.../vct-launcher{,.exe}`
///      are TRACKED files (`git ls-files launcher/dist/`), so every git
///      operation that advances the tree writes them.
///   2. Windows: a running binary is mandatory-locked, and git reverts the
///      WHOLE operation atomically on a single sharing violation.
///   3. POSIX: git replaces the inode, so a still-running hub keeps serving
///      old code from a deleted inode for the rest of the session.
///   4. install.py's post-update deploy writes fresh state files and must not
///      race the old hub's open handles.
///
/// Hard-fails when the hub will not die — see
/// [`gate_destructive_write_on_hub_stop`] for that decision and its wording.
///
/// The renames are Windows-only (`#[cfg]`-selected no-ops elsewhere) and
/// belt-and-braces even there: the hub is already stopped, but Windows can
/// retain a sharing-violation flag briefly after process exit (antivirus,
/// indexers).
///
/// `operation` is the one word both user-visible strings are derived from
/// ("update" / "merge" / "rebase" / "resume" / "resync"). `progress` receives
/// the two events at 2 % and 5 % that every migrated surface already emitted,
/// in the same order, with the same text; a surface with no progress modal
/// passes a no-op closure.
///
/// WHOEVER CALLS THIS OWES THE RESTART. The hub is down when this returns Ok;
/// `installer::ensure_hub_started_after_update` (directly, or via
/// `abort_update_restore_binaries_and_hub` on a failure path) is the other
/// half, and skipping it leaves the user with a perma-stopped hub — the
/// failure `07101d30` called out by name.
pub(crate) fn stop_hub_and_rename_binaries_aside(
    install_path: &Path,
    operation: &str,
    before: Option<&str>,
    progress: impl Fn(&str, &str, f32),
) -> Result<PrePullRenames, String> {
    progress(
        "update",
        &format!("Stopping vct-hub for {}...", operation),
        2.0,
    );
    gate_destructive_write_on_hub_stop(
        ensure_hub_stopped_for_update(install_path),
        operation,
        before,
    )?;

    progress("update", &format!("Preparing for {}...", operation), 5.0);
    // ORDER (from the original four call sites, preserved): hub binary first,
    // then the running launcher.
    Ok(PrePullRenames {
        hub: pre_pull_rename_vct_hub_binary(install_path),
        launcher: pre_pull_rename_running_binary(install_path),
    })
}


/// The one read-only refusal the hub stop can produce, run BEFORE the
/// kill-sweep so it costs nothing: `<vct_root>/hub.pid` exists but cannot be
/// read. `ensure_hub_stopped_for_update` refuses on exactly that condition
/// (it cannot tell whether a hub is running), and it used to do so AFTER the
/// sweep had already killed the MCP servers. An absent file is "no hub" and
/// passes; every other outcome of the stop (a hub that will not die) needs the
/// stop itself and stays in [`stop_hub_and_rename_binaries_aside`].
pub(crate) fn hub_stop_precheck() -> Result<(), String> {
    hub_stop_precheck_at(&vct_launcher_core::paths::vct_root_dir().join("hub.pid"))
}

/// [`hub_stop_precheck`] with the pid-file path injected (tests).
pub(crate) fn hub_stop_precheck_at(pid_file: &Path) -> Result<(), String> {
    match std::fs::read_to_string(pid_file) {
        Ok(_) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!(
            "Update refused before anything was changed: {} exists but cannot be read ({}), \
             so the launcher cannot tell whether vct-hub is running and would not be able to \
             stop it before the update. Check the file's permissions, or stop the hub with \
             `vct-hub --stop`, then try again.",
            pid_file.display(),
            e
        )),
    }
}

/// v0.2.51 Bug A: clear any leftover resume sentinel + deferral from a prior
/// half-finished update. A fresh update supersedes it — either it succeeds (no
/// resume needed) or it hits a new conflict and rewrites both with current
/// SHAs/branch. MUST run after [`run_preflight_refusals`]: a clone wedged
/// mid-merge needs the first click's sentinel to reopen its modal.
pub(crate) fn clear_prior_resume_state(install_path: &Path) {
    clear_update_resume_sentinel(install_path);
    clear_update_resume_deferral_if_solo(install_path);
}

/// V52-AI (v0.2.52): the MCP kill-sweep + the update gate, as one step.
///
/// The user reported ~97 python and ~77 node MCP processes accumulating
/// during an update (Windows mandatory file locks + Claude Code's MCP-respawn
/// loop racing the binary refresh). Strategy:
///   1. Pre-sweep: terminate currently-running MCP processes whose
///      commandlines match strict MCP patterns. Soft-fail.
///   2. Arm a RAII lockfile guard at `<vct_root>/.update-in-progress.json`;
///      the MCP servers read it at startup (`claude_mcp_servers/_lib/
///      update_gate.py`) and exit 75 during the window.
///   3. The guard's Drop deletes the lockfile on ALL exit paths.
///
/// A lockfile write failure is soft (worst case: the pre-fix fork-bomb
/// behaviour); the guard's Drop is a no-op when it never armed.
///
/// ORDER: callers run this only after every refusal that needs no mutation
/// (L2-F14) — the sweep kills processes that nothing restarts.
pub(crate) fn arm_update_gate(surface: &str) -> UpdateInProgressGuard {
    let pre_sweep_count = crate::commands::update_gate::pre_update_mcp_kill_sweep();
    if pre_sweep_count > 0 {
        tracing::info!(
            "[vct] {}: pre-sweep terminated {} MCP-shaped process(es) before update",
            surface,
            pre_sweep_count
        );
    }
    let (guard, _gate_write_result) = UpdateInProgressGuard::new();
    guard
}

/// What [`pull_to_upstream`] established.
pub(crate) struct PulledToUpstream {
    pub already_up_to_date: bool,
    pub dist_binary_stale: bool,
    pub pull_branch: String,
    pub db_audit: Vec<(String, serde_json::Value)>,
}

/// The git operation of a fast-forward update: resolve the branch, then the
/// fetch → A0 → F1 → reconcile → pull → classification → HEAD-advance
/// sequence. The hub must already be stopped and the binaries renamed aside
/// (`renames`); every failure leg below restores them before returning.
pub(crate) async fn pull_to_upstream(
    install_path: &Path,
    opts: &UpdatePipelineOptions<'_>,
    renames: &PrePullRenames,
) -> Result<PulledToUpstream, UpdatePipelineError> {
    let surface = opts.surface;
    if let Some(window) = opts.emit_progress_to {
        emit_progress(window, "update", "Pulling latest changes...", 10.0);
    }

    // Detect the current branch so the explicit `git pull <remote>
    // <branch>` invocation below doesn't depend on upstream tracking
    // config (which would point at `origin/<branch>` on a fork).
    // v0.2.92 WP-13: through the ONE resolver. A detached HEAD is logged, not
    // blocked: the pull fast-forwards a detached HEAD perfectly well.
    let pull_branch_state = match crate::commands::git_cmd::resolve_branch(install_path).await {
        Ok(s) => s,
        Err(e) => {
            abort_update_restore_binaries_and_hub(
                install_path,
                renames.launcher.as_deref(),
                renames.hub.as_deref(),
            );
            return Err(UpdatePipelineError::Raw(format!("git rev-parse failed: {}", e)));
        }
    };
    let pull_branch = pull_branch_state.name.clone();
    if pull_branch_state.detached {
        tracing::warn!(
            "[vct] {}: {} has a DETACHED HEAD — pulling {}/{}. The pull \
             advances HEAD but leaves it detached; Preferences → Launcher updates offers a \
             one-click reattach.",
            surface,
            install_path.display(),
            crate::commands::self_update::VCO_UPSTREAM_REMOTE,
            pull_branch,
        );
    }

    let sequence = reconcile_and_pull(
        install_path,
        pull_branch.clone(),
        &PullSequenceCtx {
            surface,
            emit_progress_to: opts.emit_progress_to,
            install_path_label: opts.install_path_label,
            start_branch: opts.start_branch,
            head_sha_before: opts.head_sha_before.clone(),
            update_start_ms: opts.update_start_ms,
            pre_pull_renamed: renames.launcher.clone(),
            pre_pull_renamed_hub: renames.hub.clone(),
        },
    )
    .await?;

    Ok(PulledToUpstream {
        already_up_to_date: sequence.already_up_to_date,
        dist_binary_stale: sequence.dist_binary_stale,
        pull_branch,
        db_audit: sequence.db_audit,
    })
}

/// The in-progress merge/rebase refusal (v0.2.93 field incident), before ANY
/// mutation: a clone left mid-merge reopens the conflict modal on the stalled
/// state instead of starting a pull git would refuse. Nothing is mutated on
/// this path — no sentinel clear, no kill-sweep, no hub stop, no rename.
///
/// v0.2.100 (WP-03b): the caller-specific second refusal
/// (`ExtraPreflight::RefuseDirtyTrackedAtRisk`, the dirty-tracked-file guard
/// of `apply_launcher_update`, whose only forward action was the destructive
/// resync) is retired with that surface: `ResetHard` now SAVES the working
/// tree before resetting (`update_run::create_reset_backup`) instead of
/// refusing on it.
///
/// Git reads only, so it is drivable over a temp repo.
pub(crate) async fn run_preflight_refusals(
    install_path: &Path,
    surface: &'static str,
) -> Result<(), UpdatePipelineError> {
    // v0.2.93 (field incident 2026-09-07): a merge/rebase is ALREADY in
    // progress in the clone → do NOT start a fresh pull (it would only be
    // refused with "You have not concluded your merge (MERGE_HEAD exists)",
    // pre-fix classified as a generic failure: no modal, no sentinel, and
    // the `clear_update_resume_sentinel` below would have ERASED the first
    // click's sentinel). Short-circuit to the SAME conflict payload the
    // first click produced so the conflict modal opens on the stalled
    // state. Must run BEFORE the sentinel clear, the kill-sweep, the hub
    // stop and the binary renames — none of them are appropriate for a
    // tree we are not going to touch.
    if let Some(payload) = refuse_if_merge_or_rebase_in_progress(install_path, surface).await {
        // The `_refused_merge_in_progress` audit row is the CALLER's to write:
        // every field of it is the caller's own pre-pipeline context, and it
        // lands before the caller returns, exactly as it does today.
        return Err(UpdatePipelineError::MergeInProgress {
            payload,
        });
    }

    Ok(())
}

/// The git-only core of the pipeline: fetch → A0 → F1 → generated reconcile →
/// pull-plan → pull → classification → HEAD-advance.
///
/// Split out from [`pull_to_upstream`] so it can be driven in
/// a test against a temp repo pair. The steps it does NOT contain are exactly
/// the ones a test must not run on a developer's machine: the MCP kill-sweep
/// and the hub stop both reach into the live process table.
struct PullSequenceCtx<'a> {
    surface: &'static str,
    emit_progress_to: Option<&'a Window>,
    install_path_label: &'a str,
    start_branch: &'a str,
    head_sha_before: Option<String>,
    update_start_ms: i64,
    /// Owned rather than borrowed so the abort tails below keep reading
    /// `pre_pull_renamed.as_deref()` — the expression they were moved with.
    pre_pull_renamed: Option<PathBuf>,
    pre_pull_renamed_hub: Option<PathBuf>,
}

struct PullSequenceOutcome {
    already_up_to_date: bool,
    dist_binary_stale: bool,
    db_audit: Vec<(String, serde_json::Value)>,
}

async fn reconcile_and_pull(
    repo: &Path,
    pull_branch: String,
    ctx: &PullSequenceCtx<'_>,
) -> Result<PullSequenceOutcome, UpdatePipelineError> {
    // Owned, for the same reason as in the caller above: the moved lines say
    // `&install_path`, and they still do.
    let install_path = repo.to_path_buf();
    let surface = ctx.surface;
    // The audit rows below carry the caller's own path string and its
    // pre-pipeline branch/timestamp — same names the moved code used.
    let path = ctx.install_path_label;
    let start_branch = ctx.start_branch;
    let old_sha = &ctx.head_sha_before;
    let update_start_ms = ctx.update_start_ms;
    let pre_pull_renamed = &ctx.pre_pull_renamed;
    let pre_pull_renamed_hub = &ctx.pre_pull_renamed_hub;
    let mut db_audit: Vec<(String, serde_json::Value)> = Vec::new();
    let progress = |stage: &str, message: &str, percentage: f32| {
        if let Some(window) = ctx.emit_progress_to {
            emit_progress(window, stage, message, percentage);
        }
    };


    // v0.2.24 §A0 (2026-05-22): pre-merge user-editable files BEFORE
    // `git pull --ff-only`. Without this step, ANY local uncommitted
    // edit to an allowlisted file (CLAUDE.md, .claude/CONTEXT_STATE.md,
    // knowledge/**/*.md, etc.) that ALSO has upstream changes would
    // make git pull refuse with "Your local changes would be
    // overwritten by merge" — every 3rd-party user hits this the first
    // time upstream touches those files.
    //
    // The pre-merge:
    //   1. Resolves base = merge-base(HEAD, vco_upstream/<branch>).
    //   2. Resolves theirs = vco_upstream/<branch> tip.
    //   3. Walks the diff base..theirs ∩ git status --porcelain
    //      ∩ USER_EDITABLE_PATTERNS allowlist.
    //   4. Per file: clean merge → write merged content + stage.
    //                conflict → write sidecar `<path>.from-upstream-<sha>`
    //                          leave local in place.
    //
    // Best-effort: any failure (no upstream ref yet, malformed diff,
    // git merge-file errors) is logged and skipped — the bare `git
    // pull --ff-only` below still runs and surfaces the original
    // error if pre-merge couldn't help.
    //
    // We MUST `git fetch` first: pre_merge_user_editable resolves
    // refs via `rev-parse vco_upstream/<branch>` and reads blobs via
    // `git show <sha>:<path>`; without a recent fetch the local refs
    // are stale and pre-merge sees no upstream changes.
    progress("update", "Fetching upstream for pre-merge...", 7.0);
    // v0.2.83 (D5): route through the single serialized fetch home (Quick
    // policy, branch refspec). Soft-fail posture preserved: if the fetch fails
    // the bare pull below surfaces the real error; we still attempt pre-merge
    // with whatever refs exist. The mutex + `--no-write-fetch-head` protect
    // against the FETCH_HEAD race with the startup badge check (A-RC3).
    if let Err(e) = crate::commands::upstream_fetch::serialized_fetch_upstream(
        &install_path,
        crate::commands::upstream_fetch::FetchPolicy::Quick,
        Some(&pull_branch),
    )
    .await
    {
        tracing::warn!(
            "[vct] {}: pre-merge fetch failed: {} — continuing",
            surface,
            e
        );
    }
    let pre_merge_outcomes = run_pre_merge_user_editable(&install_path, &pull_branch).await;

    // v0.2.24 §A0 (Q1 fix): emit deferral entries BEFORE the bare
    // git pull. Rationale: when pre-merge produces sidecars (true
    // 3-way conflict), the local file is still divergent and the
    // --ff-only pull WILL fail with non-FF. The user then sees the
    // B4 divergence modal — without the deferral entries on disk,
    // they lose the audit trail of which files pre-merge sidecar'd
    // vs auto-merged. Emit unconditionally so the deferral lands
    // regardless of which branch the pull takes. Best-effort: a
    // deferral-write failure must NOT block the update flow.
    maybe_emit_pre_merge_deferrals(&install_path, &pre_merge_outcomes, &pull_branch);

    // v0.2.24 §A0 (peer-review follow-up): when pre-merge produced a
    // synthetic commit (Merged outcome), local HEAD now strictly
    // advances upstream tip. A bare `git pull --ff-only` would fail
    // with non-FF for the COMMON case of a user-editable diff,
    // surfacing the B4 modal for what should be a seamless update.
    // Route through `git pull --rebase` instead: this replays the
    // synthetic pre-merge commit onto upstream tip, giving a clean
    // linear history. If the rebase has conflicts (genuine user
    // divergence beyond the allowlisted files), git falls back to
    // the existing conflict-handling path.
    //
    // When pre-merge produced no synthetic commit (all outcomes were
    // NoChange or PreservedWithUpstreamSidecar), keep the original
    // --ff-only behaviour: any non-FF in that case IS a genuine
    // divergence the user needs to confirm via the B4 modal.
    let pre_merge_committed = crate::commands::git_user_editable_merge::any_outcome_produced_synthetic_commit(
        &pre_merge_outcomes,
    );
    // v0.2.56 (Defect A fix): when pre-merge did NOT synthesize a commit
    // (so the code below would otherwise pick `--ff-only`), the LOCAL
    // clone may STILL have diverged from upstream via COMMITTED local
    // commits — the universal case for a 3rd-party user whose Claude has
    // committed KG nodes (encouraged behavior). A bare `--ff-only` then
    // refuses with non-fast-forward and surfaces the scary B4
    // Merge/Rebase/Cancel modal EVEN WHEN a real merge would be
    // conflict-free (committed KG additions never overlap upstream's
    // source/version/binary changes).
    //
    // The pre-merge step is blind to this: it only inspects `git status
    // --porcelain` (UNcommitted edits). So before settling on --ff-only,
    // probe statelessly with `git merge-tree --write-tree` (writes
    // nothing — see committed_divergence_merges_cleanly). If the merge is
    // conflict-free, route through a REAL merge pull (`--no-rebase
    // --no-edit`) instead of --ff-only: the merge completes silently, the
    // existing post-pull success flow runs unchanged, and NO modal
    // surfaces. The modal is reserved for GENUINE content conflicts (or a
    // merge that can't even start). Best-effort: any probe failure leaves
    // `--ff-only` in place so the legacy non-FF path still surfaces the
    // modal — never auto-merge on uncertainty.
    // v0.2.56 (review BLOCKER B1) + v0.2.58 (precise gate): the auto-merge
    // path uses `--autostash`, which can leave a SILENTLY-broken working
    // tree (exit 0 + UU markers + dangling stash) if an uncommitted edit
    // conflicts on the autostash pop — bypassing the post-pull conflict
    // modal. v0.2.56 guarded this with a BLUNT "working tree must be 100%
    // clean" check, but an installed orchestrator is PERMANENTLY dirty in
    // the expected way (hundreds of untracked user KG nodes + scratch
    // files): that gate bailed every real update to the scary divergence
    // modal even when the merge was perfectly safe.
    //
    // v0.2.58 narrows the gate to the PRECISE pop-conflict-risk set:
    // `tracked-modified ∩ upstream-changed`. `git stash`/`--autostash`
    // never touches UNTRACKED files, and a tracked-modified file upstream
    // didn't change can't pop-conflict — so the ONLY risky files are ones
    // both locally-modified (tracked) AND changed by upstream in this
    // merge. If that set is empty, the auto-merge is safe regardless of how
    // many untracked KG nodes / scratch files dirty the tree. This honors
    // the principle that the update must NOT CARE about expected-to-diverge
    // user files. See `tracked_modified_overlapping_upstream`. (The shared fn
    // resolves `theirs` ONCE and reuses it for both the risk check and the
    // merge-tree probe; any resolution/probe error keeps `--ff-only` so the
    // modal surfaces, never a wrong silent auto-merge.)
    //
    // v0.2.71 (Piece 3): the pull-strategy decision is now the SHARED
    // `resolve_divergence_pull_plan` in `git_user_editable_merge` (then used
    // by both update surfaces so they couldn't drift; since v0.2.100 this
    // sequence is the only caller). The decision tree is identical to the
    // pre-v0.2.71 inline block: pre_merge_committed → RebaseAutostash; else
    // resolve theirs/base + pop-conflict-risk + merge-tree probe → RealMerge
    // when clean & no risk, FfOnly otherwise (conservative on any uncertainty).
    //
    // v0.2.89 addendum (current state): a take-upstream reconcile now FRONTS
    // this decision. `resolve_generated_files_to_upstream` (wired just above,
    // after F1) resolves GENERATED / release-controlled divergence (lockfiles /
    // package.json / Cargo.lock / dist/**) to upstream BEFORE the plan runs, so
    // the merge-tree probe below sees clean end-trees for that class and routes
    // RealMerge (no modal) instead of FfOnly. Its `reconcile_committed` flag is
    // threaded into the resolver so an A0 pre-merge commit + a reconcile commit
    // together fall through to the probe rather than short-circuiting to rebase
    // (§5). The v0.2.56/58 pop-conflict-risk / merge-tree machinery described
    // above is UNCHANGED — it now just handles the SOURCE-file remainder.
    //
    // v0.2.29/v0.2.56/v0.2.58 rationale (preserved): the RebaseAutostash arm
    // uses `--autostash` so in-progress WIP outside the allowlist doesn't
    // abort the rebase ("cannot pull with rebase: You have unstaged
    // changes"); the RealMerge arm folds conflict-free committed divergence
    // (e.g. committed KG nodes) with `--autostash` LIVE over a dirty tree
    // we proved has no pop-conflict overlap. The ONE residual hazard for
    // RealMerge is a TOCTOU race (upstream pushes a commit touching a
    // locally-modified file between our pre-check and the pull's own fetch)
    // → caught by the post-pull autostash-pop backstop below, NOT silently
    // continued. NOTE (review C1): after a RealMerge, local HEAD is a merge
    // commit. (The post-tag binary-refresh window no longer re-pulls: since
    // v0.2.100 phase 11 reads the dist sidecars once — `update_run::
    // decide_binary_refresh` — and records a not-yet-published binary.)
    // v0.2.78 ITEM #0 (F1): before deciding the pull plan, auto-restore any
    // TRACKED uncommitted file whose working-tree content is byte-identical to
    // the incoming upstream blob. Such a file is not a real modification (its
    // content already == the merge target), so it should not force the
    // divergence modal via the pop-conflict-risk set. Byte-identity-gated
    // (never mtime/size); divergent files are left in the risk set → modal.
    // Shared helper (one home).
    let f1_restored =
        crate::commands::git_user_editable_merge::auto_restore_byte_identical_tracked_mods(
            &install_path,
            &pull_branch,
        )
        .await;
    if f1_restored > 0 {
        tracing::info!(
            "[vct] {}: F1 auto-restored {} byte-identical tracked file(s) \
             before divergence-plan resolution",
            surface,
            f1_restored
        );
    }
    // v0.2.89 §4.3: after F1 (byte-identical restore) and BEFORE the plan
    // resolution, reconcile GENERATED / release-controlled files to upstream
    // (take-upstream bias) — lockfiles, package.json, Cargo.lock, dist/**.
    // F1 first is cheap (it may byte-identically restore an allowlisted path
    // for free); the reconcile then handles the byte-different remainder so
    // the plan resolver below sees a cleaned tree/history and does NOT surface
    // the modal for the "expected conflict" class (dep-bump / lockfile / dist
    // divergence). A divergent SOURCE file still surfaces the modal (real
    // breakage signal). Best-effort throughout: any per-file failure leaves
    // that file divergent → it stays in the modal-forcing sets (never worse
    // than today). Shared helper — the self_update surface calls the SAME fn.
    let gen_reconcile =
        crate::commands::git_user_editable_merge::resolve_generated_files_to_upstream(
            &install_path,
            &pull_branch,
        )
        .await;
    if gen_reconcile.reconcile_committed
        || !gen_reconcile.took_upstream.is_empty()
        || !gen_reconcile.restored_worktree.is_empty()
    {
        tracing::info!(
            "[vct] {}: reconciled generated/release-controlled file(s) to \
             upstream — {} committed take-upstream, {} worktree-restored (reconcile_committed={})",
            surface,
            gen_reconcile.took_upstream.len(),
            gen_reconcile.restored_worktree.len(),
            gen_reconcile.reconcile_committed
        );
        // v0.2.89 MINOR-1: the audit-trail deferral (`generated_files_reconciled`)
        // is NOT emitted here — emitting injects a reminder block into the tracked
        // CLAUDE.md, which pre-pull would dirty CLAUDE.md between this reconcile and
        // the pull-plan decision below and could self-inflict the divergence modal.
        // It is emitted AFTER the pull succeeds (search MINOR-1 below).
    }
    let pull_plan = crate::commands::git_user_editable_merge::resolve_divergence_pull_plan(
        &install_path,
        &pull_branch,
        pre_merge_committed,
        // v0.2.89 §5: when the reconcile created a synthetic take-upstream
        // commit, do NOT short-circuit to the rebase arm even if A0 also
        // committed — fall through to the merge-tree probe (rebase would
        // replay the fork's original dep-bump commit which conflicts
        // regardless; the merge arm folds the clean end-trees).
        gen_reconcile.reconcile_committed,
    )
    .await;
    // Retained for the conflict-op label below (the post-pull conflict +
    // autostash-pop paths say "merge" for the real-merge arm, "rebase"
    // otherwise) — identical semantics to the pre-v0.2.71 boolean.
    let auto_merge_committed_divergence = pull_plan
        == crate::commands::git_user_editable_merge::PullPlan::RealMerge;
    let pull_args =
        pull_plan.pull_args(crate::commands::self_update::VCO_UPSTREAM_REMOTE, &pull_branch);
    // v0.2.95 phase 2 — `LC_ALL=C` is NEW here, and it is a fix, not a tidy-up.
    //
    // Every classifier this function feeds (`is_merge_in_progress_refusal`,
    // `is_untracked_overwrite_abort`, `is_merge_or_rebase_conflict`,
    // `is_non_fast_forward`) matches ENGLISH substrings of git's output. This
    // surface has always run git in the user's locale, so on a non-English git
    // every one of them silently returns false and a real conflict degrades to
    // the generic raw-error path — `is_non_fast_forward`'s own doc admits it
    // ("the launcher does not run git with a forced locale … so this is
    // best-effort").
    //
    // The OTHER surface did not have that hole: `apply_launcher_update` pulled
    // through `git_cmd::run_git_combined`, which pins `LC_ALL=C` for exactly
    // this reason (v0.2.71 LOW-4). Folding it onto this pipeline without the
    // pin would have REGRESSED it, so the pin comes along — and the surface
    // that lacked it gains what the other one already had. `run_git_raw_env`
    // exists for precisely this kind of override.
    let pull = match crate::commands::git_cmd::run_git_raw_env(
        &install_path,
        &pull_args,
        &[("LC_ALL", "C")],
    )
    .await
    {
        Ok(out) => out,
        Err(e) => {
            // v0.2.95 ship-gate MINOR-3: a SPAWN failure is not a pull that
            // ran and failed — but the tree is in exactly the same state,
            // because everything before this point already happened. The hub
            // is STOPPED and both binaries are RENAMED ASIDE. Returning
            // straight out left the user with no hub and, on Windows, a
            // launcher whose canonical path holds nothing.
            //
            // Its documented sibling twelve lines down (the
            // `!pull.status.success()` branch) restores both. Two exits from
            // the same statement disagreeing about whether the setup is owed a
            // teardown is the bug; they agree now.
            abort_update_restore_binaries_and_hub(
                &install_path,
                pre_pull_renamed.as_deref(),
                pre_pull_renamed_hub.as_deref(),
            );
            return Err(UpdatePipelineError::Raw(format!("git pull failed: {}", e)));
        }
    };

    if !pull.status.success() {
        let stderr = String::from_utf8_lossy(&pull.stderr);
        let stdout = String::from_utf8_lossy(&pull.stdout);
        // v0.2.17 (plan 0.0.B): on pull failure, revert the pre-pull
        // rename so the running launcher can still be re-launched if
        // the user kills the GUI. Best-effort. v0.2.21 Step 12 also reverts
        // the hub-binary rename + restarts the hub. v0.2.71: shared tail.
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );

        // v0.2.51 Bug A (defensive): the rebase-with-autostash branch can
        // produce a rebase conflict (`CONFLICT (content):` lines on the
        // synthetic pre-merge commit OR on the user's WIP via autostash
        // pop). Detect that before falling through to the non-FF /
        // generic-error paths so the conflict modal surfaces correctly +
        // the resume sentinel lands.
        //
        // v0.2.56: the new `auto_merge_committed_divergence` path runs a
        // `--no-rebase` MERGE pull, which on the rare probe-vs-pull TOCTOU
        // race can ALSO conflict. Label the operation accurately so the
        // resume sentinel + modal say "merge" not "rebase". (The abort
        // recovery `abort_orchestrator_merge_or_rebase` is label-agnostic
        // — it reads .git/MERGE_HEAD vs .git/rebase-merge on disk — so
        // this is for the user-facing message only, but accuracy matters.)
        let combined = format!("{}\n{}", stderr, stdout);

        // v0.2.93 (field incident 2026-09-07): git REFUSED to pull because a
        // merge/rebase is already in progress (a TOCTOU sibling of the
        // pre-pull short-circuit above — e.g. a terminal `git merge` started
        // between the probe and the pull). Tested FIRST: the unmerged-files
        // variant ends in "unresolved conflict", which the conflict
        // classifier below would otherwise claim as a FRESH conflict.
        // Routes to the SAME conflict payload (sentinel written only if
        // absent) so the modal opens on the stalled state.
        if crate::commands::git_user_editable_merge::is_merge_in_progress_refusal(&combined) {
            let fallback_op = if auto_merge_committed_divergence {
                "merge"
            } else {
                "rebase"
            };
            return Err(UpdatePipelineError::MergeInProgress {
                payload: handle_merge_in_progress_refusal(
                    &install_path,
                    surface,
                    &pull_branch,
                    fallback_op,
                    &combined,
                )
                .await,
            });
        }

        // v0.2.88 (F2-followup / FIELD DEFECT): the untracked-overwrite abort
        // MUST be caught BEFORE `is_merge_or_rebase_conflict`. Both match the
        // "would be overwritten by" substring, but this abort happens BEFORE any
        // merge starts, so `collect_conflicted_files` (which reads UNMERGED INDEX
        // entries) returns EMPTY — the field bug where the conflict modal
        // rendered zero files and no actionable resolution, degrading to a bare
        // FAILED toast. The colliding paths ARE in the stderr, unparsed.
        //
        // The pull sequence (pre-v0.2.100: `update_orchestrator`'s inline pull)
        // is the ONE path that lacked the
        // pre-pull `handle_untracked_collisions_pre_pull` guard (which only
        // fronts the modal-triggered merge/rebase resolvers). This POST-pull
        // classifier closes that gap by parsing the file list out of the stderr
        // and routing to the dedicated untracked-collision handler + event. The
        // upstream tip was already fetched (serialized_fetch_upstream above), so
        // `compute_theirs_sha` resolves and the byte-identity classification is
        // exact. Best-effort: a parse-empty / resolution-empty result falls
        // through to the existing conflict/non-FF paths → never worse than today.
        if crate::commands::git_user_editable_merge::is_untracked_overwrite_abort(&combined) {
            // v0.2.88 (NIT-10): pass the ACTUAL pull-plan op, not a hardcoded
            // "merge". A rebase-plan abort otherwise mislabels the payload's
            // `operation` field + the deferral prose as "merge" (git's own abort
            // message even says "by checkout" for a rebase). Same
            // RealMerge→"merge" / else→"rebase" mapping the conflict/pop paths use.
            let overwrite_op = if auto_merge_committed_divergence {
                "merge"
            } else {
                "rebase"
            };
            if let Some(payload) = handle_untracked_overwrite_post_pull(
                &install_path,
                overwrite_op,
                &pull_branch,
                &combined,
            )
            .await
            {
                return Err(UpdatePipelineError::UntrackedCollision { payload });
            }
            // Parse yielded nothing actionable → fall through to the legacy
            // paths below (raw error), never a wrong action.
        }

        if is_merge_or_rebase_conflict(&combined) {
            let conflict_op = if auto_merge_committed_divergence {
                "merge"
            } else {
                "rebase"
            };
            // v0.2.53 DEDUP-14: paired sentinel + deferral via the
            // single helper so future writers can't accidentally write
            // one without the other (v0.2.51 Bug A class).
            let sentinel_written =
                write_resume_sentinel_and_deferral(&install_path, conflict_op, &pull_branch)
                    .await;
            let conflicted = collect_conflicted_files(&install_path).await;
            log_conflict_payload_return(surface, conflict_op, &conflicted, sentinel_written);
            return Err(UpdatePipelineError::Conflict {
                operation: conflict_op,
                branch: pull_branch.clone(),
                conflicted,
                detail: combined.trim().to_string(),
                // This path discards the abort tail's outcome (it did, before
                // the extraction, and a new row is not phase 1's business).
                record_binary_clobber_averted: false,
            });
        }

        // v0.2.23 (B4 / D19): non-fast-forward branch. The user's local
        // clone has diverged from upstream (typical when they've edited
        // CLAUDE.md / CONTEXT_STATE.md / KG nodes locally, or when we
        // rewrote upstream history). Surface a structured payload so
        // the frontend can render a "Merge / Rebase / Cancel" modal
        // instead of dumping a raw git error to a toast.
        //
        // Best-effort: collect SHAs + a list of diverged files so the
        // user can see what's about to be merged. Any failure here
        // falls back to the legacy raw-error path — never block.
        if crate::commands::self_update::is_non_fast_forward(&stderr) {
            let local_sha = read_head_sha(&install_path).await;
            let remote_sha = read_remote_sha(&install_path, &pull_branch).await;
            // v0.2.93: three sets, not two — `diverged` is now the real
            // intersection (both sides touched), `upstream_only` the rest.
            let DivergedFiles {
                diverged,
                upstream_only,
                local_only,
            } = collect_diverged_files(&install_path, &pull_branch).await;
            // v0.2.55 (durable-logging fix): the non-FF case previously
            // surfaced ONLY as the GUI Merge/Rebase/Cancel modal below. If
            // the user dismisses/cancels it, the update silently didn't
            // apply and there was NO record a terminal Claude could find.
            // Write a durable UPDATE_DEFERRED.md entry too (the conflict
            // path already does this via write_resume_sentinel_and_deferral;
            // this closes the non-FF asymmetry). Best-effort: never blocks
            // the structured error the frontend needs.
            write_launcher_update_diverged_deferral(
                &install_path,
                &pull_branch,
                LauncherUpdateDivergedKind::NonFastForward {
                    local_sha: local_sha.clone(),
                    remote_sha: remote_sha.clone(),
                    detail: stderr.trim().to_string(),
                },
            );
            // The PAYLOAD is the surface's own shape (three Svelte modals parse
            // it); the pipeline hands over the CLASSIFICATION plus the three
            // file sets it just computed.
            return Err(UpdatePipelineError::NonFastForward {
                branch: pull_branch.clone(),
                local_sha,
                remote_sha,
                diverged,
                upstream_only,
                local_only,
                detail: stderr.trim().to_string(),
            });
        }
        // v0.2.55 (audit R1): any OTHER git-pull failure (not a conflict,
        // not a non-FF divergence) — e.g. a broken local git, a missing or
        // misconfigured upstream remote, an interrupted prior git operation
        // leaving `.git/MERGE_HEAD` / `.git/rebase-*`, or an unreadable
        // object store. PRE-v0.2.55 this returned a GUI-only error string
        // with no durable trace; a 3rd-party's Claude couldn't see it at
        // session start. Write a durable deferral too.
        //
        // v0.2.92 WP-13 — CORRECTION. This comment used to list "a detached
        // HEAD" among the causes. It is not one: `git pull --ff-only
        // <remote> main` fast-forwards a detached HEAD perfectly well
        // (verified empirically in a throwaway repo — HEAD advances and
        // stays detached). Nothing in this block detects detachment either;
        // the attribution was a guess, and it survived long enough to be
        // repeated verbatim in user-facing recovery text, sending a real
        // user to check a state that was not their problem. A comment
        // naming a cause is a claim about behaviour, and it gets verified
        // like one.
        write_launcher_update_diverged_deferral(
            &install_path,
            &pull_branch,
            LauncherUpdateDivergedKind::GitPullFailed {
                detail: stderr.trim().to_string(),
            },
        );
        log_generic_pull_failure(surface, "git pull", &stderr);
        return Err(UpdatePipelineError::Raw(format!(
            "git pull failed: {}",
            stderr
        )));
    }

    // v0.2.58 (review BLOCKER-1): the `--autostash` pull can SUCCEED (exit 0)
    // yet leave the tree broken. `git pull --no-rebase/--rebase --autostash`
    // stashes local tracked changes, merges/rebases, then POPS the stash. If
    // the pop conflicts, git prints "Applying autostash resulted in
    // conflicts." and leaves `UU` markers + a dangling autostash — but STILL
    // EXITS 0. The `!pull.status.success()` block above therefore does NOT
    // catch it, and proceeding would run install.py + restart on a
    // silently-broken tree (the original B1 hazard).
    //
    // This can happen on a TOCTOU race: our pop-conflict-risk pre-check saw
    // no overlap, but upstream pushed a commit touching a locally-modified
    // file in the window before the pull's own fetch. (It also covers the
    // pre-existing `--rebase --autostash` arm, which had the same latent
    // hole.) Detect it on the SUCCESS path — unmerged files present and/or
    // the autostash-conflict marker in stdout — and route to the conflict
    // modal + resume sentinel exactly like the non-zero conflict branch,
    // instead of silently continuing. Best-effort; never auto-proceed on a
    // tree we can't confirm clean.
    //
    // v0.2.89 addendum (current state): the generated/release-controlled
    // reconcile (wired above, after F1) now FRONTS this backstop for the
    // generated-file class — it restores those paths to HEAD before the pull,
    // removing them from the pop-conflict-risk set, so a locally-rebuilt dist
    // binary / regenerated lockfile no longer reaches this autostash-pop
    // backstop. This block still guards the residual SOURCE-file TOCTOU race
    // (its logic is unchanged).
    {
        let pull_combined = format!(
            "{}\n{}",
            String::from_utf8_lossy(&pull.stdout),
            String::from_utf8_lossy(&pull.stderr)
        );
        let autostash_pop_failed = pull_combined.contains("autostash resulted in conflicts")
            || pull_combined.contains("Applying autostash");
        let unmerged = collect_conflicted_files(&install_path).await;
        // v0.2.88 (NIT-12): the autostash marker can appear in git's output even
        // when the pop LEFT NO unmerged index entries (e.g. "Applying autostash"
        // on a clean apply). Routing to a conflict/pop modal on that shape shows
        // "0 file(s)" with live buttons whose only effect would be a blind stash
        // drop (the shape MAJOR-1 now also refuses). The index is the authority:
        // NO unmerged entries ⇒ nothing to resolve ⇒ do NOT emit any conflict/pop
        // modal; log and fall through to the normal path. The block is entered
        // ONLY when the index actually carries unmerged entries.
        if autostash_pop_failed && unmerged.is_empty() {
            tracing::warn!(
                "[vct] {}: autostash marker present but the index has \
                 NO unmerged entries — the pop left the tree clean; not emitting a \
                 (would-be-empty) conflict modal, continuing the update.",
                surface
            );
        }
        if !unmerged.is_empty() {
            // v0.2.88 (DEFECT 2 / FIELD DEFECT): distinguish "the merge/rebase
            // itself conflicted" from "the merge SUCCEEDED but the --autostash
            // POP of the local WIP stash conflicted". In the pop-conflict case,
            // git prints "Merge made by the 'ort' strategy" (or fast-forward)
            // BEFORE "Applying autostash resulted in conflicts. Your changes are
            // safe in the stash." Labeling this as a merge failure is the field
            // bug — the update's merge is DONE; only the local-WIP restore
            // clashed (a user hand-edited a tracked file this release touched).
            // Emit the DISTINCT `orchestrator_autostash_pop_conflict` event so
            // the modal can say so honestly and offer keep-updated / keep-local.
            let merge_succeeded = pull_combined.contains("Merge made by")
                || pull_combined.contains("Fast-forward")
                || pull_combined.contains("Successfully rebased");
            let pop_conflict_after_success = autostash_pop_failed && merge_succeeded;

            tracing::warn!(
                "[vct] {}: pull exited 0 but the working tree has \
                 {} unmerged file(s){} — {}. Routing to the {} modal.",
                surface,
                unmerged.len(),
                if autostash_pop_failed { " + autostash-conflict marker" } else { "" },
                if pop_conflict_after_success {
                    "the merge SUCCEEDED, only the autostash pop conflicted"
                } else {
                    "an --autostash pop conflict (TOCTOU race)"
                },
                if pop_conflict_after_success { "autostash-pop" } else { "conflict" },
            );
            // Restore the running binary + hub (we renamed/stopped pre-pull)
            // so the user can keep using the launcher after they resolve.
            //
            // v0.2.91 WI-3/WI-7: THIS is the RC-1 site. When the merge landed,
            // the pull already wrote the NEW binary to the canonical path and
            // the restore below now declines to rename the old exe back over
            // it. Record the averted clobber in the audit log too — the
            // durable deferral is written inside the tail.
            let restore = abort_update_restore_binaries_and_hub(
                &install_path,
                pre_pull_renamed.as_deref(),
                pre_pull_renamed_hub.as_deref(),
            );
            // The `update_binary_clobber_averted` row travels out ON the error:
            // the Db handle belongs to the caller, and the row's
            // `pop_conflict_after_success` field is exactly which of the two
            // branches below is taken — so the variant identity carries it
            // rather than a second copy of this decision.
            let record_clobber = restore.clobber_averted;

            if pop_conflict_after_success {
                // The merge landed; only the WIP restore clashed. Write a
                // pop-conflict deferral (distinct condition id) + return the
                // distinct event — NOT the generic conflict event that would
                // mislabel a completed merge as a failure.
                write_autostash_pop_conflict_deferral(&install_path, &pull_branch, &unmerged);
                // Also write a resume sentinel so the resolution command
                // (resolve_autostash_pop_and_retry) can delegate to the standard
                // resume tail (install.py + binary refresh) after the user picks
                // keep-updated / keep-local. `sha_at_conflict = old_sha` (the
                // pre-merge HEAD) is guaranteed DIFFERENT from the now-advanced
                // HEAD, so resume's HEAD-advance guard passes. Labeled
                // "autostash-pop" to keep the operation semantics honest.
                let sentinel_written = match old_sha.as_deref() {
                    Some(old) => write_update_resume_sentinel(
                        &install_path,
                        "autostash-pop",
                        &pull_branch,
                        old,
                    ),
                    None => false,
                };
                log_conflict_payload_return(
                    surface,
                    "autostash-pop",
                    &unmerged,
                    sentinel_written,
                );
                return Err(UpdatePipelineError::AutostashPopConflict {
                    branch: pull_branch.clone(),
                    conflicted: unmerged,
                    detail: pull_combined.trim().to_string(),
                    record_binary_clobber_averted: record_clobber,
                });
            }

            let conflict_op = if auto_merge_committed_divergence {
                "merge"
            } else {
                "rebase"
            };
            let sentinel_written =
                write_resume_sentinel_and_deferral(&install_path, conflict_op, &pull_branch)
                    .await;
            log_conflict_payload_return(surface, conflict_op, &unmerged, sentinel_written);
            return Err(UpdatePipelineError::Conflict {
                operation: conflict_op,
                branch: pull_branch.clone(),
                conflicted: unmerged,
                detail: pull_combined.trim().to_string(),
                record_binary_clobber_averted: record_clobber,
            });
        }
    }

    let pull_output = String::from_utf8_lossy(&pull.stdout);
    if pull_output.contains("Already up to date") {
        progress("done", "Already up to date!", 100.0);
        // v0.2.17: nothing was pulled — revert the rename so the canonical
        // path holds the (still-current) binary. The user doesn't expect a
        // restart in this case. v0.2.21 Step 12: same for the hub binary,
        // then bring it back up — the existing binary starts cleanly since
        // nothing changed on disk. v0.2.71: shared tail.
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        // v0.2.91 WI-2: "Already up to date" MUST STILL HEAL.
        //
        // Pre-v0.2.91 this branch returned success here and
        // `finalize_update_and_restart` — and with it ALL staging/handoff
        // machinery — was never reached. An install whose source is current
        // but whose dist binary is stale (the field case: a hand-copied exe
        // after a failed swap) therefore had NO path back to a fresh binary:
        // every subsequent update said "Already up to date" and changed
        // nothing, forever.
        //
        // Ordering note (deliberate deviation from the plan's literal
        // wording): the revert runs FIRST, then the reconcile. On this branch
        // the pull wrote nothing, so the pre-pull rename left the canonical
        // path EMPTY — reconciling before the revert would stage against a
        // missing file and leave the canonical path absent until the next
        // quit. Reverting first restores a working binary; the reconcile then
        // sees the true at-rest state and stages on top of it.
        let heal = crate::services::binary_freshness::reconcile_dist_at_rest(&install_path).await;
        // v0.2.43 V0243-15: audit complete for the no-op path.
        db_audit.push((
            format!("{}_complete", surface),
            serde_json::json!({
                "success": true,
                "duration_ms": chrono::Utc::now().timestamp_millis() - update_start_ms,
                "note": "already_up_to_date",
                "branch": start_branch,
                "binary_stale": heal.is_stale(),
                "binaries_staged": heal.staged,
                "swap_armed": heal.armed,
            }),
        ));
        // The user-facing MESSAGE is the surface's own wording and its result
        // shape is the surface's own — the caller builds both from
        // `dist_binary_stale`.
        return Ok(PullSequenceOutcome {
            already_up_to_date: true,
            dist_binary_stale: heal.is_stale(),
            db_audit,
        });
    }

    progress("update", "Changes pulled", 30.0);

    // v0.2.24 §A0 (Q1 fix): deferrals were already emitted BEFORE the
    // pull (see above) — no second call needed here.

    // v0.2.89 MINOR-1: emit the generated-file reconcile audit deferral HERE —
    // AFTER the pull succeeded, NOT inside resolve_generated_files_to_upstream.
    // Emitting injects a reminder block into the tracked CLAUDE.md; doing it
    // pre-pull would dirty CLAUDE.md between the reconcile and the pull-plan
    // decision and could self-inflict the divergence modal on an otherwise-clean
    // fork. Best-effort (no-op when nothing was reconciled). The deferral
    // self-clears on the imminent install.py --update below.
    crate::commands::git_user_editable_merge::emit_generated_reconcile_deferrals(
        &install_path,
        &gen_reconcile,
    );

    // v0.2.63: HEAD-advance backstop. A pull that exited 0 but did NOT reach
    // the upstream tip (a non-FF that slipped through, an odd partial state)
    // must NOT proceed to install.py — that would run the STALE tree (the
    // v0.2.62 GUI-update crash class: old install.py at pre-fix line numbers).
    // Abort cleanly, write a durable deferral so a terminal Claude can see the
    // update didn't land, and return a plain error (NOT the Merge/Rebase modal
    // — that path is what failed; routing back to it would loop).
    match assert_head_reached_upstream(&install_path).await {
        Ok(HeadAdvanceOutcome::Reached) => {}
        // v0.2.92 WP-13 (item 8): the guard fail-opens, but no longer in
        // silence. The update continues — see the rationale on the enum — and
        // a durable `launcher_update_post_pull_unverified` entry tells the
        // user (and their terminal Claude at session start) that the one check
        // proving the pull landed could not run.
        Ok(HeadAdvanceOutcome::Unverified { error }) => {
            crate::commands::git_user_editable_merge::write_launcher_update_post_pull_unverified_deferral(
                &install_path,
                &pull_branch,
                &error,
            );
            db_audit.push((
                format!("{}_post_pull_unverified", surface),
                serde_json::json!({
                    "branch": pull_branch,
                    "error": error,
                    "install_path": path,
                }),
            ));
        }
        Err(e) => {
        abort_update_restore_binaries_and_hub(
            &install_path,
            pre_pull_renamed.as_deref(),
            pre_pull_renamed_hub.as_deref(),
        );
        write_launcher_update_diverged_deferral(
            &install_path,
            &pull_branch,
            LauncherUpdateDivergedKind::NonFastForward {
                local_sha: read_head_sha(&install_path).await,
                remote_sha: read_remote_sha(&install_path, &pull_branch).await,
                detail: e.clone(),
            },
        );
        // The `_complete` failure row is the CALLER's: every field of it is
        // the caller's own pre-pipeline context.
        return Err(UpdatePipelineError::HeadDidNotAdvance { detail: e });
        }
    }

    Ok(PullSequenceOutcome {
        already_up_to_date: false,
        dist_binary_stale: false,
        db_audit,
    })
}

// ---------------------------------------------------------------------------
// v0.2.100 WP-03b (F-W2-06): the Merge / Rebase / Resume git operations
// ---------------------------------------------------------------------------
//
// Moved out of the three legacy `#[command]`s (`merge_orchestrator_with_
// upstream`, `rebase_orchestrator_onto_upstream`, `resume_orchestrator_update`)
// that each ran their own copy of the update tail. Here they are ONLY the git
// operation of phase 6 of `update_run::run_update`: the hub stop, the binary
// renames, install.py, the restart and every recovery are the pipeline's.
// None of these functions restores the binaries or restarts the hub — the
// driver does, once, on every failure leg.

/// What a divergence-recovery git operation established.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RecoveryGitOp {
    /// Nothing was merged/replayed — install.py is not run.
    pub already_up_to_date: bool,
    pub branch: String,
}

fn progress(window: Option<&Window>, message: &str, pct: f32) {
    if let Some(w) = window {
        emit_progress(w, "update", message, pct);
    }
}

/// A merge/rebase that stopped on conflicts: write the resume sentinel +
/// deferral (the badge's "Continue update"), then report the condition.
async fn stopped_on_conflict(
    install_path: &Path,
    surface: &'static str,
    operation: &'static str,
    branch: &str,
    conflicted: Vec<String>,
    detail: &str,
) -> UpdatePipelineError {
    let sentinel_written = write_resume_sentinel_and_deferral(install_path, operation, branch).await;
    log_conflict_payload_return(surface, operation, &conflicted, sentinel_written);
    UpdatePipelineError::Conflict {
        operation,
        branch: branch.to_string(),
        conflicted,
        detail: detail.trim().to_string(),
        record_binary_clobber_averted: false,
    }
}

/// `git pull --autostash` can EXIT 0 with the stash pop conflicted (`UU`
/// entries + a dangling stash). The index is the authority: unmerged entries
/// route to the conflict modal; an autostash marker with none is a clean pop.
async fn unmerged_after_success(install_path: &Path, surface: &'static str, output: &str) -> Vec<String> {
    let marker = output.contains("autostash resulted in conflicts")
        || output.contains("Applying autostash")
        || output.contains("could not apply autostash");
    let unmerged = collect_conflicted_files(install_path).await;
    if marker && unmerged.is_empty() {
        tracing::warn!(
            "[vct] {}: autostash marker present but the index has NO unmerged entries — \
             the pop left the tree clean; continuing.",
            surface
        );
    }
    unmerged
}

fn combined_output(out: &std::process::Output) -> String {
    format!(
        "{}\n{}",
        String::from_utf8_lossy(&out.stderr),
        String::from_utf8_lossy(&out.stdout)
    )
}

/// `Merge`: merge `vco_upstream/<branch>` into the diverged clone
/// (`git pull --no-rebase --no-edit --autostash`), after the §A0 pre-merge of
/// user-editable files, the untracked-collision pre-pass and the
/// generated-file reconcile — the same preparation the legacy command ran.
pub(crate) async fn merge_upstream(
    install_path: &Path,
    surface: &'static str,
    window: Option<&Window>,
) -> Result<RecoveryGitOp, UpdatePipelineError> {
    let branch = crate::commands::installer::resolve_pull_branch(install_path).await;
    progress(window, "Fetching upstream for pre-merge...", 7.0);
    // Soft-fail: a failed fetch falls through to the pull, which reports it.
    if let Err(e) = crate::commands::upstream_fetch::serialized_fetch_upstream(
        install_path,
        crate::commands::upstream_fetch::FetchPolicy::Quick,
        Some(&branch),
    )
    .await
    {
        tracing::warn!("[vct] {}: pre-merge fetch failed: {} — continuing", surface, e);
    }
    let pre_merge = run_pre_merge_user_editable(install_path, &branch).await;
    maybe_emit_pre_merge_deferrals(install_path, &pre_merge, &branch);

    progress(window, "Checking for untracked-file collisions...", 8.0);
    crate::commands::installer::handle_untracked_collisions_pre_pull(install_path, "merge", &branch)
        .await
        .map_err(|payload| UpdatePipelineError::UntrackedCollision { payload })?;

    progress(window, "Reconciling generated files to upstream...", 9.0);
    let gen_reconcile =
        crate::commands::git_user_editable_merge::resolve_generated_files_to_upstream(
            install_path,
            &branch,
        )
        .await;

    // --no-rebase: git 2.34+ refuses a divergent pull without an explicit
    // reconcile mode. --no-edit: no tty for the merge message. --autostash:
    // the divergence modal is reached on dirty trees (v0.2.71).
    progress(window, "Merging upstream into local...", 10.0);
    let pull = crate::commands::git_cmd::run_git_raw(
        install_path,
        &[
            "pull",
            "--no-rebase",
            "--no-edit",
            "--autostash",
            crate::commands::self_update::VCO_UPSTREAM_REMOTE,
            branch.as_str(),
        ],
    )
    .await
    .map_err(|e| UpdatePipelineError::Raw(format!("git pull (merge) failed: {}", e)))?;
    let combined = combined_output(&pull);

    if !pull.status.success() {
        // The first click's merge is still in progress (v0.2.93 incident).
        if crate::commands::git_user_editable_merge::is_merge_in_progress_refusal(&combined) {
            let payload =
                handle_merge_in_progress_refusal(install_path, surface, &branch, "merge", &combined)
                    .await;
            return Err(UpdatePipelineError::MergeInProgress {
                payload,
            });
        }
        if is_merge_or_rebase_conflict(&combined) {
            let conflicted = collect_conflicted_files(install_path).await;
            return Err(
                stopped_on_conflict(install_path, surface, "merge", &branch, conflicted, &combined)
                    .await,
            );
        }
        let stderr = String::from_utf8_lossy(&pull.stderr).to_string();
        log_generic_pull_failure(surface, "git pull (merge)", &stderr);
        return Err(UpdatePipelineError::Raw(format!(
            "git pull (merge) failed: {}",
            stderr.trim()
        )));
    }

    let unmerged = unmerged_after_success(install_path, surface, &combined).await;
    if !unmerged.is_empty() {
        return Err(
            stopped_on_conflict(install_path, surface, "merge", &branch, unmerged, &combined).await,
        );
    }
    if String::from_utf8_lossy(&pull.stdout).contains("Already up to date") {
        return Ok(RecoveryGitOp {
            already_up_to_date: true,
            branch,
        });
    }
    // After the merge landed, not before: the audit deferral injects a block
    // into the tracked CLAUDE.md (v0.2.89 MINOR-1).
    crate::commands::git_user_editable_merge::emit_generated_reconcile_deferrals(
        install_path,
        &gen_reconcile,
    );
    Ok(RecoveryGitOp {
        already_up_to_date: false,
        branch,
    })
}

/// `Rebase`: replay the clone's local commits onto `vco_upstream/<branch>`
/// (`git rebase --autostash`), after a HARD fetch (the rebase needs the ref)
/// and the untracked-collision pre-pass.
pub(crate) async fn rebase_onto_upstream(
    install_path: &Path,
    surface: &'static str,
    window: Option<&Window>,
) -> Result<RecoveryGitOp, UpdatePipelineError> {
    let branch = crate::commands::installer::resolve_pull_branch(install_path).await;
    progress(window, "Fetching upstream for rebase...", 10.0);
    crate::commands::upstream_fetch::serialized_fetch_upstream(
        install_path,
        crate::commands::upstream_fetch::FetchPolicy::Quick,
        Some(&branch),
    )
    .await
    .map_err(|e| UpdatePipelineError::Raw(format!("git fetch failed: {}", e)))?;

    progress(window, "Checking for untracked-file collisions...", 15.0);
    crate::commands::installer::handle_untracked_collisions_pre_pull(install_path, "rebase", &branch)
        .await
        .map_err(|payload| UpdatePipelineError::UntrackedCollision { payload })?;

    progress(window, "Rebasing local onto upstream...", 20.0);
    let upstream_ref = format!(
        "{}/{}",
        crate::commands::self_update::VCO_UPSTREAM_REMOTE,
        branch
    );
    let rebase = crate::commands::git_cmd::run_git_raw(
        install_path,
        &["rebase", "--autostash", upstream_ref.as_str()],
    )
    .await
    .map_err(|e| UpdatePipelineError::Raw(format!("git rebase failed: {}", e)))?;
    let combined = combined_output(&rebase);

    if !rebase.status.success() {
        if is_merge_or_rebase_conflict(&combined) {
            let conflicted = collect_conflicted_files(install_path).await;
            return Err(
                stopped_on_conflict(install_path, surface, "rebase", &branch, conflicted, &combined)
                    .await,
            );
        }
        let stderr = String::from_utf8_lossy(&rebase.stderr).to_string();
        log_generic_pull_failure(surface, "git rebase", &stderr);
        return Err(UpdatePipelineError::Raw(format!(
            "git rebase failed: {}",
            stderr.trim()
        )));
    }
    let unmerged = unmerged_after_success(install_path, surface, &combined).await;
    if !unmerged.is_empty() {
        return Err(
            stopped_on_conflict(install_path, surface, "rebase", &branch, unmerged, &combined)
                .await,
        );
    }
    let up_to_date = combined.contains("is up to date");
    Ok(RecoveryGitOp {
        already_up_to_date: up_to_date,
        branch,
    })
}

/// `Resume`'s verdict on the clone, decided from the resume sentinel and git
/// state. Read-only: preflight asks it (refusals before any mutation) and the
/// git operation asks it again (the tree may have moved in between).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ResumeVerdict {
    /// A resolved merge/rebase awaits install.py.
    Proceed { branch: String },
    /// HEAD never moved past the conflict SHA and the clone is not behind:
    /// the merge was aborted and there is nothing to resume or apply.
    NothingPending { branch: String },
    /// Refused. `stale_sentinel`: the sentinel provably describes nothing
    /// (HEAD unchanged) — the caller clears it so the badge stops offering a
    /// resume that cannot happen.
    Refuse {
        code: &'static str,
        message: String,
        stale_sentinel: bool,
    },
}

pub(crate) async fn classify_resume(install_path: &Path) -> ResumeVerdict {
    use crate::commands::installer::{
        classify_resume_noop, detect_remaining_conflict_markers, read_update_resume_sentinel,
        resume_head_unchanged_is_benign_noop, ResumeNoopKind,
    };
    let refuse = |code, message: String, stale_sentinel| ResumeVerdict::Refuse {
        code,
        message,
        stale_sentinel,
    };
    let Some(sentinel) = read_update_resume_sentinel(install_path) else {
        return refuse(
            "resume_not_pending",
            "No resume pending: no update halted at a conflict in this clone. Refresh the \
             update status; nothing was changed."
                .into(),
            false,
        );
    };
    if sentinel.sha_at_conflict.is_empty() {
        return refuse(
            "resume_sentinel_unverifiable",
            "The resume record is missing the conflict-time commit, so the clone's baseline \
             cannot be verified and the resume is refused. Run the normal update (it writes a \
             fresh record), or finish/abort the merge with `git status` in the clone."
                .into(),
            false,
        );
    }
    let head = read_head_sha(install_path).await.unwrap_or_default();
    let branch = if sentinel.branch.is_empty() {
        crate::commands::installer::resolve_pull_branch(install_path).await
    } else {
        sentinel.branch.clone()
    };
    if resume_head_unchanged_is_benign_noop(&sentinel.sha_at_conflict, &head) {
        let remote = crate::commands::self_update::VCO_UPSTREAM_REMOTE;
        return match crate::commands::git_cmd::commits_behind(install_path, remote, &branch).await {
            Err(e) => refuse(
                "resume_state_unverifiable",
                format!(
                    "Nothing was resumed, and the update state could not be verified (couldn't \
                     count commits behind {}/{}: {}). Run the normal update to be sure you are \
                     on the latest version.",
                    remote, branch, e
                ),
                true,
            ),
            Ok(behind) => match classify_resume_noop(behind) {
                ResumeNoopKind::TrulyNothingPending => ResumeVerdict::NothingPending { branch },
                ResumeNoopKind::UpdateStillPending { behind } => refuse(
                    "resume_nothing_to_resume",
                    format!(
                        "Nothing to resume — the earlier update never applied (HEAD is still \
                         {} commit(s) behind {}/{}). Run the normal update to fetch and \
                         install the new version.",
                        behind, remote, branch
                    ),
                    true,
                ),
            },
        };
    }
    let markers = detect_remaining_conflict_markers(install_path).await;
    if !markers.is_empty() {
        let head_list = markers.iter().take(8).cloned().collect::<Vec<_>>().join(", ");
        let more = if markers.len() > 8 { ", …" } else { "" };
        let how = if sentinel.operation == "autostash-pop" {
            "For EACH file pick one side — `git checkout --ours -- <file>` (keep the update) or \
             `git checkout --theirs -- <file>` (keep your edit) — then `git add -- <file>` and \
             `git stash drop 'stash@{0}'` (see .claude/context/UPDATE_DEFERRED.md). Do NOT \
             `git commit --amend` — the merge already committed."
        } else {
            "Remove the `<<<<<<<` / `=======` / `>>>>>>>` markers, `git add` + commit the fix."
        };
        return refuse(
            "resume_conflict_markers_remain",
            format!(
                "Found unresolved conflict markers in {} file(s): {}{}. {} Then continue the \
                 update again.",
                markers.len(),
                head_list,
                more,
                how
            ),
            false,
        );
    }
    ResumeVerdict::Proceed { branch }
}

// ---------------------------------------------------------------------------
// `install.py --update` — the artefact-application step, one home
// ---------------------------------------------------------------------------

/// What `install.py --update` did. Everything the callers branch on, and
/// nothing they do not: the recovery (revert the binaries, restart the hub,
/// reopen the DB) is each caller's, because each holds different things open.
///
/// v0.2.100 (WP-03a, L2-F10): the failure REASON travels with the run. The
/// docstring used to promise "stdout is deliberately NOT carried … no caller
/// branches on it"; that was true, and it is exactly why a failure whose only
/// text was on stdout (or a signal death with empty stderr) reached the user as
/// "Update failed: Update failed:". The promise is superseded by the reader
/// that now exists — `update_failure::failure_message` — which is why the exit
/// facts and a BOUNDED stdout tail are carried below.
pub(crate) struct InstallPyRun {
    pub success: bool,
    /// install.py's full stderr. The legacy surfaces still quote it; the one
    /// pipeline renders [`crate::commands::update_failure::failure_message`].
    pub stderr: String,
    /// `ExitStatus::code()` — `None` when the process was killed by a signal.
    pub exit_code: Option<i32>,
    /// The terminating signal on Unix when `exit_code` is `None`.
    pub signal: Option<i32>,
    /// The last `[N/M]` step banner install.py printed, from the FULL output
    /// (computed before the stdout tail is cut, so an early marker survives).
    pub last_step: Option<String>,
    /// The last [`STDOUT_TAIL_MAX_LINES`] lines of stdout (at most
    /// [`STDOUT_TAIL_MAX_BYTES`]) — the failure reason when stderr is empty.
    pub stdout_tail: String,
}

/// Bound on the stdout tail [`InstallPyRun`] keeps. install.py's stdout is the
/// whole install transcript (thousands of lines); only its end explains a
/// failure, and the message it feeds is a modal, not a log.
pub(crate) const STDOUT_TAIL_MAX_LINES: usize = 40;
/// Byte bound on the same tail (a single pathological line cannot blow it up).
pub(crate) const STDOUT_TAIL_MAX_BYTES: usize = 16 * 1024;

/// The last `max_lines` lines of `text`, then at most `max_bytes` from the end
/// (cut on a char boundary). Pure; one home for "bounded tail".
pub(crate) fn bounded_tail(text: &str, max_lines: usize, max_bytes: usize) -> String {
    let lines: Vec<&str> = text.trim_end().lines().collect();
    let start = lines.len().saturating_sub(max_lines);
    let joined = lines[start..].join("\n");
    if joined.len() <= max_bytes {
        return joined;
    }
    let mut cut = joined.len() - max_bytes;
    while !joined.is_char_boundary(cut) {
        cut += 1;
    }
    joined[cut..].to_string()
}

/// The terminating signal of a finished process, when there is one.
fn exit_signal(status: &std::process::ExitStatus) -> Option<i32> {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        status.signal()
    }
    #[cfg(not(unix))]
    {
        let _ = status;
        None
    }
}

/// Run `python install.py --update` in `install_path`, record the outcome, and
/// hand the caller the facts.
///
/// ONE HOME, because this is ledger step 31 — "artefact application" was a true
/// duplicate of intent with three implementations, and phase 2 adds a fourth
/// caller (`self_update::apply_launcher_update`, which until then applied
/// artefacts with `cargo build` instead and shipped a half-updated install).
/// Writing a fourth copy is the thing the project's modularity rule forbids, so
/// the two `--update` spawns that differ only in whether they stream progress
/// were collapsed into this, and the new caller is the third call site.
///
/// `stream_to`: `Some(window)` mirrors install.py's `[VCO-EVENT] <step> <phase>
/// <detail>` lines to the progress modal as sub-messages (holding the parent
/// percentage steady at 50 %, between the caller's 40 % "Applying" and its 90 %
/// "Starting vct-hub"), and is the ONLY thing that sets `VCO_PROGRESS_STREAM`.
/// `None` waits for the process. A surface with no progress modal must not
/// claim one — install.py branches on that env var.
///
/// Recording is unconditional and happens HERE so no caller can forget it: a
/// SPAWN failure gets `record_install_spawn_failure` (v0.2.93 MINOR-3 — a
/// spawn failure after a successful pull is the same partly-updated state as a
/// non-zero exit), and every exit goes through `handle_install_phase_exit`,
/// which records a failure or settles a prior one.
pub(crate) async fn run_install_py_update(
    install_path: &Path,
    python_cmd: &str,
    surface: &str,
    stream_to: Option<&Window>,
) -> Result<InstallPyRun, String> {
    // v0.2.95 phase 3 (WP-3): the ONE home for the spawn setup — interpreter,
    // `install.py`, `.silent()`, stdin-null, cwd and the UTF-8 pair.
    let mut cmd = crate::commands::installer::install_py_command(
        python_cmd,
        install_path,
        ["--update"],
    );
    // v0.2.15 (Agent D): install.py names this PID in the
    // `launcher_restart_required` deferral. Absence is a soft fallback.
    cmd.env("VCT_LAUNCHER_PID", std::process::id().to_string());
    // v0.2.17 (plan 0.0): "the Rust side is handling the restart" —
    // install.py then skips the now-redundant `launcher_restart_required`
    // deferral. A manual terminal run (no env) still emits it.
    cmd.env("VCT_AUTO_RESTART_LAUNCHER", "1");
    if stream_to.is_some() {
        // v0.2.49 batch 4: mirror `_log_install_event` to stdout so the
        // multi-minute re-embed phase is not a static "Applying updates…".
        cmd.env("VCO_PROGRESS_STREAM", "1");
    }
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());

    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(e) => {
            let msg = format!("install.py --update failed to spawn: {}", e);
            record_install_spawn_failure(install_path, install_path, surface, &msg);
            return Err(msg);
        }
    };

    // v0.2.95 ship-gate MINOR-2 — BOTH pipes are drained concurrently, and
    // that is a correctness requirement rather than a tidiness one.
    //
    // This used to read stdout to EOF, then `wait()`, then drain stderr under
    // the comment "small; the process has already exited". Neither half of
    // that sentence is guaranteed: install.py's stderr carries pip resolver
    // noise, Python warnings and any traceback, and the OS pipe buffer is
    // ~64 KiB. Once install.py has written that much stderr its next write
    // BLOCKS — so it never exits, never closes stdout, the `next_line()` loop
    // never sees EOF, and `wait()` is never reached. The update hangs forever
    // with the hub stopped and the binaries renamed aside; the user sees
    // "Applying updates…" and nothing else, on every one of the three surfaces
    // that now share this home.
    //
    // Draining both at once removes the coupling: stderr can never fill,
    // because it is being read the whole time. `tokio::join!` runs them on
    // this one task — no thread, no spawn, no ordering assumption.
    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();

    // v0.2.96 WP-8 (register issue 4, GUI half): the stall watchdog. Only
    // meaningful when there IS a progress surface to inform, and only
    // spawned when not disabled (`VCO_UPDATE_STALL_WARN_SECS=0`). State and
    // done-flag live OUTSIDE the futures: read_stdout feeds one, the
    // watchdog loop reads both, `tokio::join!` shares the `&` borrows.
    let stall_warn_secs = resolve_stall_warn_secs(
        std::env::var("VCO_UPDATE_STALL_WARN_SECS").ok().as_deref(),
    );
    let watchdog_state = match (stream_to, stall_warn_secs) {
        (Some(_), 0) => {
            tracing::debug!(
                "[vct] {}: stall watchdog disabled via VCO_UPDATE_STALL_WARN_SECS=0",
                surface
            );
            None
        }
        (Some(_), secs) => Some(std::sync::Arc::new(std::sync::Mutex::new(
            StallWatchdogState::new(std::time::Duration::from_secs(secs)),
        ))),
        (None, _) => None,
    };
    let watchdog_done = watchdog_state
        .as_ref()
        .map(|_| std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)));

    let read_stdout = async {
        let mut buf = Vec::<u8>::new();
        let Some(stdout) = stdout_pipe else { return buf };
        use tokio::io::{AsyncBufReadExt, BufReader};
        let mut reader = BufReader::new(stdout).lines();
        loop {
            match reader.next_line().await {
                Ok(Some(line)) => {
                    buf.extend_from_slice(line.as_bytes());
                    buf.push(b'\n');
                    // ANY stdout line is heartbeat. install.py itself is
                    // silent while it blocks on the seed child — the
                    // producer there is the CHILD's own throttled
                    // `[VCO-EVENT] kg-sync …` ticks (emitted by
                    // sync_knowledge_graph.py under VCO_PROGRESS_STREAM,
                    // relayed onto install.py's stdout by
                    // vco_lib/child_process.py), which is what keeps a
                    // slow but healthy re-embed from looking stalled.
                    if let Some(state) = watchdog_state.as_ref() {
                        state
                            .lock()
                            .unwrap_or_else(|p| p.into_inner())
                            .note_progress();
                    }
                    let Some(window) = stream_to else { continue };
                    // MUST MATCH `vco_lib/progress_event.py` — `EVENT_PREFIX`
                    // (this literal, trailing space included) and
                    // `EVENT_FIELD_COUNT` (the `splitn` below). That module is
                    // the ONE Python home for the grammar; both producers
                    // (`install.py::_log_install_event`,
                    // `templates/scripts/sync_knowledge_graph.py::_emit_sync_event`)
                    // and the relay filter
                    // (`vco_lib/child_process.py::_EVENT_LINE_RE`) build their
                    // lines from it. Parity is enforced by
                    // `tests/test_v0296_lane_python_core.py::VcoEventLineGrammarParity`,
                    // which reads THIS file and compares — not a marker scan.
                    // Change either side alone and every progress tick stops
                    // reaching the modal, which looks identical to a stalled
                    // update.
                    let Some(rest) = line.strip_prefix("[VCO-EVENT] ") else {
                        continue;
                    };
                    // Format: `<step> <phase> <detail...>`. Only `start` / `ok`
                    // become progress messages — warn/error/skip stay silent
                    // here; they are already in the JSONL log + the failure
                    // path's stderr.
                    let mut parts = rest.splitn(3, ' ');
                    let step = parts.next().unwrap_or("");
                    let phase = parts.next().unwrap_or("");
                    let detail = parts.next().unwrap_or("");
                    if phase != "start" && phase != "ok" {
                        continue;
                    }
                    let sub_msg = installer_step_to_user_label(step, detail);
                    if !sub_msg.is_empty() {
                        emit_progress(window, "install", &sub_msg, 50.0);
                    }
                }
                Ok(None) => break, // EOF
                Err(e) => {
                    tracing::warn!(
                        "[vct] {}: install.py stdout read error: {} (continuing; \
                         install.py still running)",
                        surface,
                        e
                    );
                    break;
                }
            }
        }
        // stdout is at EOF — the update has finished producing output, so
        // the watchdog must stand down within one tick rather than fire a
        // bogus stall notice on a process that already exited.
        if let Some(done) = watchdog_done.as_ref() {
            done.store(true, std::sync::atomic::Ordering::Relaxed);
        }
        buf
    };

    let read_stderr = async {
        let mut buf = Vec::<u8>::new();
        if let Some(mut stderr) = stderr_pipe {
            use tokio::io::AsyncReadExt;
            let _ = stderr.read_to_end(&mut buf).await;
        }
        buf
    };

    let run_watchdog = async {
        if let (Some(state), Some(done), Some(window)) = (
            watchdog_state.as_ref(),
            watchdog_done.as_ref(),
            stream_to,
        ) {
            stall_watchdog_loop(
                std::time::Duration::from_secs(stall_warn_secs),
                std::time::Duration::from_millis(250),
                std::sync::Arc::clone(state),
                std::sync::Arc::clone(done),
                |msg: String| {
                    // Same install-progress channel the [VCO-EVENT] parser
                    // feeds (see `read_stdout` above); the message is short
                    // by construction to honour the GUI's <200-char detail
                    // contract (progress.rs unknown-step fallback).
                    emit_progress(window, "install", &msg, 50.0);
                    tracing::warn!("[vct] {}: {}", surface, msg);
                },
            )
            .await;
        }
    };

    let (stdout_buf, stderr_buf, ()) = tokio::join!(read_stdout, read_stderr, run_watchdog);

    // Both pipes are at EOF, so the child has closed them — `wait()` reaps a
    // process that is already finishing rather than one we are still starving.
    let status = child
        .wait()
        .await
        .map_err(|e| format!("install.py --update wait failed: {}", e))?;

    let stdout = String::from_utf8_lossy(&stdout_buf).into_owned();
    let stderr = String::from_utf8_lossy(&stderr_buf).into_owned();

    // v0.2.93: record a failed install phase (ERROR log + action_required
    // ledger row) or settle a prior one on success.
    handle_install_phase_exit(
        install_path,
        install_path,
        surface,
        status.success(),
        status.code(),
        &stdout,
        &stderr,
    );

    let last_step = crate::commands::installer::classify_install_phase_exit(
        false,
        status.code(),
        &stdout,
        &stderr,
    )
    .and_then(|f| f.last_step);
    Ok(InstallPyRun {
        success: status.success(),
        stderr,
        exit_code: status.code(),
        signal: exit_signal(&status),
        last_step,
        stdout_tail: bounded_tail(&stdout, STDOUT_TAIL_MAX_LINES, STDOUT_TAIL_MAX_BYTES),
    })
}

// ─── v0.2.96 WP-8 (register issue 4, GUI half): the update stall watchdog ──
//
// Watches install.py's stdout for progress. install.py emits its own
// `[VCO-EVENT]` lines only BETWEEN phases — while it blocks on the seed
// child it prints nothing, and the producer there is the CHILD:
// sync_knowledge_graph.py emits throttled `[VCO-EVENT] kg-sync …` ticks
// (gated on VCO_PROGRESS_STREAM, which this pipeline threads through
// install.py's env into the seed child) and the WP-1 relay
// (`vco_lib/child_process.py`) forwards them onto install.py's stdout.
// With that producer in place a healthy update produces stdout lines
// even mid-seed, so N minutes of total silence means the user is looking
// at a modal that may never move again (the 2026-09-20 "stuck at
// Seeding" class, now deadlock-free but still potentially slow).
//
// The watchdog only ever SURFACES a state ("update may be stalled") on the
// same install-progress channel the [VCO-EVENT] parser feeds. It never
// aborts, kills, or times anything out — the no-global-timeout ruling
// (v0.2.69 FIX 3) stands; a genuinely slow re-embed must complete.

/// Default silence before the "may be stalled" notice: 10 minutes.
const DEFAULT_STALL_WARN_SECS: u64 = 600;
/// Floor: a configured stall window below this is treated as misconfigured
/// noise (a sub-minute stall notice would fire during normal pip resolves).
const MIN_STALL_WARN_SECS: u64 = 30;

/// Pure env resolution: `VCO_UPDATE_STALL_WARN_SECS` — default 600, `0`
/// disables the watchdog entirely, garbage falls back to the default
/// (never disables on a typo), anything below the floor is clamped up.
pub(crate) fn resolve_stall_warn_secs(env_val: Option<&str>) -> u64 {
    let Some(raw) = env_val else { return DEFAULT_STALL_WARN_SECS };
    match raw.trim().parse::<u64>() {
        Ok(0) => 0,
        Ok(n) => n.max(MIN_STALL_WARN_SECS),
        Err(_) => DEFAULT_STALL_WARN_SECS,
    }
}

/// The stall decision state, pure and unit-tested.
#[derive(Debug)]
struct StallWatchdogState {
    stall: std::time::Duration,
    last_progress: std::time::Instant,
    last_notice: Option<std::time::Instant>,
}

impl StallWatchdogState {
    fn new(stall: std::time::Duration) -> Self {
        Self {
            stall,
            last_progress: std::time::Instant::now(),
            last_notice: None,
        }
    }

    /// Any stdout line counts as progress — the heartbeat contract.
    fn note_progress(&mut self) {
        self.last_progress = std::time::Instant::now();
    }

    /// Should a stall notice fire at `now`? At most one notice per stall
    /// window of CONTINUED silence (the state stays surfaced; it is not a
    /// toast storm).
    fn notice_due(&mut self, now: std::time::Instant) -> bool {
        if now.duration_since(self.last_progress) < self.stall {
            return false;
        }
        match self.last_notice {
            Some(t) if now.duration_since(t) < self.stall => false,
            _ => {
                self.last_notice = Some(now);
                true
            }
        }
    }
}

/// The watchdog loop, parameterised on the emit closure so tests can
/// observe notices without a tauri `Window` (none is constructible in
/// tests). Exits within one `tick` of `done` being set — it never cancels
/// or interferes with the reader it watches.
async fn stall_watchdog_loop<F>(
    stall: std::time::Duration,
    tick: std::time::Duration,
    shared: std::sync::Arc<std::sync::Mutex<StallWatchdogState>>,
    done: std::sync::Arc<std::sync::atomic::AtomicBool>,
    mut emit: F,
) where
    F: FnMut(String),
{
    loop {
        tokio::time::sleep(tick).await;
        if done.load(std::sync::atomic::Ordering::Relaxed) {
            return;
        }
        let notice = {
            let mut state = shared.lock().unwrap_or_else(|p| p.into_inner());
            state.notice_due(std::time::Instant::now())
        };
        if notice {
            let mins = stall.as_secs() / 60;
            let label = if mins > 0 {
                format!("{} min", mins)
            } else {
                format!("{} s", stall.as_secs())
            };
            emit(format!(
                "update may be stalled — no progress for {label} (still running; \
                 nothing was cancelled; you can keep waiting or abort from the UI)"
            ));
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::git_user_editable_merge::tests::{
        init_repo_pair, push_upstream_change,
    };
    use std::process::{Command as StdCommand, Stdio};

    fn git_missing() -> bool {
        StdCommand::new("git")
            .arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| !s.success())
            .unwrap_or(true)
    }

    /// The interpreter name `run_install_py_update` would be handed, or `None`
    /// when this machine has neither spelling on PATH.
    fn python_cmd_for_tests() -> Option<&'static str> {
        for candidate in ["python3", "python"] {
            let ok = StdCommand::new(candidate)
                .arg("--version")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map(|s| s.success())
                .unwrap_or(false);
            if ok {
                return Some(candidate);
            }
        }
        None
    }

    /// v0.2.95 ship-gate MINOR-2 — the stderr deadlock, as a real child.
    ///
    /// The pre-fix reader drained stdout to EOF, THEN `wait()`ed, THEN read
    /// stderr, under the comment "small; the process has already exited".
    /// A child that writes more than the OS pipe buffer (~64 KiB on Linux) to
    /// stderr blocks on that write, so it never exits, never closes stdout,
    /// and the stdout loop never sees EOF — the update hangs forever with the
    /// hub stopped and the binaries renamed aside.
    ///
    /// The fake `install.py` here is the minimal reproduction: 512 KiB of
    /// stderr BEFORE anything on stdout. Against the fixed reader it returns
    /// in well under a second; against the pre-fix reader the `timeout` below
    /// expires and `must not hang` is the named failure.
    ///
    /// Nothing here touches the network, Weaviate, the hub or a real install:
    /// the "install root" is a `tempdir` containing one throwaway script, and
    /// `stream_to` is `None` so no progress window is involved.
    #[tokio::test]
    async fn install_py_stderr_beyond_the_pipe_buffer_does_not_deadlock_the_reader() {
        let Some(python) = python_cmd_for_tests() else {
            eprintln!("skipping: no python on PATH");
            return;
        };
        let td = tempfile::tempdir().expect("tempdir");
        let root = td.path();
        // 512 KiB — eight times the usual pipe capacity, so the child is
        // certain to block if nobody is reading stderr.
        std::fs::write(
            root.join("install.py"),
            "import sys\n\
             sys.stderr.write('x' * (512 * 1024))\n\
             sys.stderr.flush()\n\
             sys.stdout.write('done\\n')\n\
             sys.stdout.flush()\n",
        )
        .unwrap();

        let run = tokio::time::timeout(
            std::time::Duration::from_secs(60),
            run_install_py_update(root, python, "minor2_deadlock_regression", None),
        )
        .await
        .expect(
            "run_install_py_update must not hang when install.py fills the stderr \
             pipe — draining stdout first and stderr only after wait() deadlocks",
        )
        .expect("the run itself must succeed");

        assert!(run.success, "the fake install.py exits 0");
        assert!(
            run.stderr.len() >= 512 * 1024,
            "the whole of stderr must be captured, not just the first pipeful \
             (got {} bytes)",
            run.stderr.len(),
        );
    }

    /// v0.2.100 WP-03a (L2-F10): a failing install.py's reason travels with
    /// the run — exit code, the last `[N/M]` marker from the FULL output (an
    /// early marker survives the tail cut), and a BOUNDED stdout tail — so a
    /// stdout-only failure is no longer rendered as an empty reason.
    #[tokio::test]
    async fn install_py_run_carries_exit_code_step_marker_and_bounded_stdout_tail() {
        let Some(python) = python_cmd_for_tests() else {
            eprintln!("skipping: no python on PATH");
            return;
        };
        let td = tempfile::tempdir().expect("tempdir");
        std::fs::write(
            td.path().join("install.py"),
            "import sys\n\
             print('[5/10] Starting services')\n\
             for i in range(200):\n    print('noise line %d' % i)\n\
             print('ERROR: compose up failed')\n\
             sys.stdout.flush()\n\
             sys.exit(3)\n",
        )
        .unwrap();
        let run = run_install_py_update(td.path(), python, "wp03a_stdout_tail", None)
            .await
            .expect("spawned");
        assert!(!run.success);
        assert_eq!(run.exit_code, Some(3));
        assert_eq!(run.signal, None);
        assert_eq!(run.last_step.as_deref(), Some("[5/10]"), "marker from the full output");
        assert!(run.stdout_tail.ends_with("ERROR: compose up failed"), "{:?}", run.stdout_tail);
        assert!(run.stdout_tail.lines().count() <= STDOUT_TAIL_MAX_LINES);
        assert!(!run.stdout_tail.contains("[5/10]"), "the tail is bounded, the marker is not");
    }

    #[test]
    fn bounded_tail_keeps_the_end_by_lines_then_bytes() {
        let text = (1..=10).map(|i| format!("l{i}")).collect::<Vec<_>>().join("\n");
        assert_eq!(bounded_tail(&text, 3, 1000), "l8\nl9\nl10");
        assert_eq!(bounded_tail(&text, 3, 3), "l10");
        assert_eq!(bounded_tail("", 3, 10), "");
        // never splits a multi-byte char
        assert_eq!(bounded_tail("é€", 1, 3), "€");
    }

    /// The hub-stop pre-check refuses ONLY an unreadable pid file — the one
    /// refusal of the hub stop decidable without killing anything — and lets
    /// an absent or readable one through (leave-alone).
    #[test]
    fn hub_stop_precheck_refuses_only_an_unreadable_pid_file() {
        let td = tempfile::tempdir().unwrap();
        let pid = td.path().join("hub.pid");
        assert!(hub_stop_precheck_at(&pid).is_ok(), "absent = no hub");
        std::fs::write(&pid, "12345\n").unwrap();
        assert!(hub_stop_precheck_at(&pid).is_ok(), "readable = the stop decides");
        std::fs::remove_file(&pid).unwrap();
        std::fs::create_dir(&pid).unwrap(); // a directory cannot be read as a file
        let err = hub_stop_precheck_at(&pid).expect_err("unreadable refuses");
        assert!(err.contains("before anything was changed"), "{err}");
    }

    /// The rendered body an install writes over the tracked stub: the user's
    /// own text OUTSIDE the AUTO markers plus the rendered template between
    /// them. That outside text is uncommitted and exists NOWHERE else, which is
    /// what makes keeping the working-tree copy load-bearing.
    fn rendered_claude_md() -> String {
        "# My project rules\n\nNEVER lose this line.\n\n\
         <!-- BEGIN: AUTO (rendered) -->\nAUTO v1 body\n<!-- END: AUTO -->\n\n\
         ## My tail section\n"
            .to_string()
    }

    fn blob_at_head(repo: &Path, path: &str) -> String {
        let out = StdCommand::new("git")
            .args(["show", &format!("HEAD:{}", path)])
            .current_dir(repo)
            .output()
            .expect("git show");
        assert!(
            out.status.success(),
            "git show HEAD:{} failed: {}",
            path,
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).into_owned()
    }

    fn test_ctx<'a>() -> PullSequenceCtx<'a> {
        PullSequenceCtx {
            surface: "test_pipeline",
            emit_progress_to: None,
            install_path_label: "<test>",
            start_branch: "main",
            head_sha_before: None,
            update_start_ms: 0,
            pre_pull_renamed: None,
            pre_pull_renamed_hub: None,
        }
    }

    /// WIRING RED-PROOF for the step this extraction could most easily have
    /// dropped in silence.
    ///
    /// `resolve_rendered_files_keep_local` reaches the INSTALLER surface only
    /// TRANSITIVELY: `reconcile_and_pull` → `run_pre_merge_user_editable` →
    /// `git_user_editable_merge::pre_merge_user_editable` →
    /// `resolve_rendered_files_keep_local_at`. Grep either update command for
    /// the rendered reconcile and you find nothing, which is exactly why an
    /// extraction that "looks equivalent" can drop the A0 call and leave every
    /// existing test green — `rendered_reconcile_keeps_local_and_the_pull_then_
    /// lands_without_conflict` calls the reconcile DIRECTLY and so proves only
    /// that the helper works, never that this pipeline reaches it.
    ///
    /// This test drives the REAL pull sequence over a temp repo pair in the
    /// state every installed clone is in (CLAUDE.md rendered over its tracked
    /// stub, uncommitted) against a release that also edited that stub — the
    /// 0.2.93→0.2.94 shape. It asserts the OBSERVABLE consequence of the reach:
    /// the pull lands, the user's rendered bytes survive, and HEAD carries
    /// upstream's blob. Delete the `run_pre_merge_user_editable` call from
    /// `reconcile_and_pull` and the pull is refused instead
    /// ("Your local changes to the following files would be overwritten by
    /// merge: CLAUDE.md") — `outcome.is_ok()` fails.
    #[tokio::test]
    async fn pull_sequence_reaches_the_rendered_reconcile_through_the_a0_step() {
        if git_missing() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");

        // The install renders CLAUDE.md over the tracked stub (uncommitted —
        // the universal state of an installed clone).
        let rendered = rendered_claude_md();
        std::fs::write(local.join("CLAUDE.md"), &rendered).unwrap();

        // The release edits the tracked stub AND ships an ordinary source
        // change, so the pull has real work to do either way.
        push_upstream_change(&seed, &local, "CLAUDE.md", "# stub v2\nPointer only.\n");
        push_upstream_change(&seed, &local, "vco_lib/foo.py", "def upstream(): pass\n");

        let outcome = reconcile_and_pull(&local, "main".to_string(), &test_ctx()).await;

        assert!(
            outcome.is_ok(),
            "the pull must LAND on a clone whose rendered CLAUDE.md diverges \
             from a stub upstream also changed — that is what the A0 step's \
             rendered reconcile is for. Without it git refuses the pull."
        );
        let outcome = outcome.unwrap_or_else(|_| unreachable!());
        assert!(
            !outcome.already_up_to_date,
            "the fixture pushes two upstream commits; this must be a real pull"
        );

        // The user's rendered copy — including the text outside the AUTO
        // markers, which lives nowhere else — survived the pull.
        assert_eq!(
            std::fs::read_to_string(local.join("CLAUDE.md")).unwrap(),
            rendered,
            "the working-tree rendered copy must survive the whole sequence"
        );
        // ...and HEAD carries upstream's tracked stub. ONLY the rendered
        // reconcile inside the A0 pre-merge advances the tracked blob like
        // this; a plain merge would have conflicted or been refused.
        assert_eq!(
            blob_at_head(&local, "CLAUDE.md"),
            "# stub v2\nPointer only.\n",
            "HEAD's tracked CLAUDE.md must be upstream's blob — the observable \
             signature of the rendered reconcile having run"
        );
        // The ordinary source change arrived too: the pull really completed.
        assert_eq!(
            blob_at_head(&local, "vco_lib/foo.py"),
            "def upstream(): pass\n",
            "the release's source change must be in the tree"
        );
    }

    // -----------------------------------------------------------------
    // The hub-stop gate (phase 3)
    // -----------------------------------------------------------------
    //
    // BOTH ARMS of the branch that gates the destructive tree-write on every
    // update kind. The act it gates differs per kind — a `git pull`, a
    // `git rebase`, `ResetHard`'s `git reset --hard` — but the
    // decision is one, which is why it is a pure fn: the arms are drivable
    // without ever touching the live process table.

    /// ACT ARM. Both `Ok` shapes mean the same thing — nothing holds the
    /// binaries — and the caller proceeds.
    #[test]
    fn the_hub_stop_gate_lets_the_write_proceed_when_no_hub_holds_the_binaries() {
        assert!(
            gate_destructive_write_on_hub_stop(Ok(true), "update", Some("git pull")).is_ok(),
            "a hub that WAS stopped must not block the update"
        );
        assert!(
            gate_destructive_write_on_hub_stop(Ok(false), "resync", Some("git reset --hard"))
                .is_ok(),
            "no hub running at all must not block the resync — `Ok(false)` is \
             the common case on a machine whose hub is not started"
        );
    }

    /// REFUSE ARM — the one that matters. A hub that provably will not die
    /// means `launcher/dist/<arch>/vct-hub{,.exe}` (a TRACKED file) is held
    /// open: on Windows the whole git operation aborts atomically, on POSIX
    /// the surviving hub serves old code from a deleted inode. The refusal
    /// must reach the user naming BOTH what was aborted and what to do.
    #[test]
    fn the_hub_stop_gate_refuses_the_write_when_the_hub_survives() {
        let err = gate_destructive_write_on_hub_stop(
            Err("pid 4242 still alive after SIGKILL".to_string()),
            "resync",
            Some("git reset --hard"),
        )
        .expect_err("a surviving hub must REFUSE the destructive write");

        assert!(err.starts_with("Resync aborted:"), "wrong opening: {err}");
        assert!(
            err.contains("before git reset --hard"),
            "the refusal must name the act it prevented: {err}"
        );
        assert!(
            err.contains("pid 4242 still alive after SIGKILL"),
            "the underlying cause must survive into the message: {err}"
        );
        assert!(
            err.contains("vct-hub --stop"),
            "the refusal must tell the user what to run: {err}"
        );
    }

    /// The four migrated surfaces' wording, reproduced from `operation`
    /// alone. Pinned because phase 3 collapsed four hand-written copies into
    /// one derivation, and a derivation that silently reworded any of them
    /// would change what a user sees in a modal that has shipped for four
    /// releases.
    #[test]
    fn every_migrated_surface_keeps_the_refusal_wording_it_shipped_with() {
        let boom = || Err("hub would not die".to_string());
        let cases: [(&str, Option<&str>, &str); 4] = [
            ("update", Some("git pull"),
             "Update aborted: could not stop vct-hub before git pull: hub would not die. \
              Try again, or run `vct-hub --stop` manually."),
            ("merge", Some("git pull"),
             "Merge aborted: could not stop vct-hub before git pull: hub would not die. \
              Try again, or run `vct-hub --stop` manually."),
            ("rebase", Some("git rebase"),
             "Rebase aborted: could not stop vct-hub before git rebase: hub would not die. \
              Try again, or run `vct-hub --stop` manually."),
            // Resume runs no git command at all — its next step is install.py —
            // so it has always had the bare clause. `None` is what preserves it.
            ("resume", None,
             "Resume aborted: could not stop vct-hub: hub would not die. \
              Try again, or run `vct-hub --stop` manually."),
        ];
        for (operation, before, expected) in cases {
            let got = gate_destructive_write_on_hub_stop(boom(), operation, before)
                .expect_err("the fixture refuses");
            assert_eq!(got, expected, "wording drifted for `{operation}`");
        }
    }

    // -----------------------------------------------------------------
    // The pre-flight refusals (phase 2)
    // -----------------------------------------------------------------
    //
    // These are the both-arms tests this project requires of a branch that
    // gates a destructive act. The acts gated here are the MCP kill-sweep, the
    // HUB STOP, the two binary renames and the pull itself — everything
    // the pipeline does after `run_preflight_refusals`
    // returns Ok. The refusal path is the "leave alone" arm, and it is the one
    // that matters: it is why a launcher update no longer stops the user's hub
    // before discovering it was never going to pull.
    //
    // They run against a temp repo pair and touch NOTHING on the developer's
    // machine — which is the whole reason the refusals were split into their
    // own function rather than tested through the full pipeline.

    /// Fixture: a clone whose tracked `README.md` is locally modified AND
    /// changed upstream — the v0.2.58 risk set `tracked-modified ∩
    /// upstream-changed`, i.e. content a destructive resync would destroy.
    fn clone_with_a_dirty_tracked_file_upstream_also_changed() -> (tempfile::TempDir, PathBuf) {
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");
        push_upstream_change(&seed, &local, "README.md", "# upstream rewrote this\n");
        std::fs::write(local.join("README.md"), "# my local edit, uncommitted\n").unwrap();
        (tmp, local)
    }


    /// The v0.2.58 NARROWING, on the arm that asks: a dirty tracked file
    /// upstream did NOT touch is not at risk, and refusing on it is the blunt
    /// proxy that made this surface refuse every orchestrator-root install.
    #[tokio::test]
    async fn preflight_allows_a_dirty_tracked_file_upstream_did_not_change() {
        if git_missing() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let (tmp, _remote, local) = init_repo_pair();
        let seed = tmp.path().join("seed");
        // Upstream changes something ELSE, so there is a real pull to do.
        push_upstream_change(&seed, &local, "vco_lib/foo.py", "def upstream(): pass\n");
        std::fs::write(local.join("README.md"), "# dirty, but nobody upstream cares\n").unwrap();

        let outcome = run_preflight_refusals(&local, "test_pipeline").await;
        assert!(
            outcome.is_ok(),
            "only `tracked-modified ∩ upstream-changed` is at risk; got {}",
            describe(&outcome)
        );
    }

    /// ORDERING, and it is the reason the caller's refusal moved INTO the
    /// pipeline rather than staying at its call site.
    ///
    /// A clone wedged mid-merge is dirty in the one way that trips the
    /// launcher surface's guard — the merge left `UU` entries on tracked paths
    /// upstream also changed. Run that guard first and the user is told to
    /// "commit, stash or revert" the very file the wedge produced, with no
    /// mention of the merge; that was `apply_launcher_update`'s behaviour, and
    /// it is a dead end because neither committing nor stashing resolves a
    /// merge. The in-progress refusal must win.
    #[tokio::test]
    async fn a_wedged_merge_is_reported_as_such_even_on_the_surface_that_also_refuses_dirt() {
        if git_missing() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let (_tmp, local) = clone_with_a_dirty_tracked_file_upstream_also_changed();
        // Wedge it, the way a terminal `git merge` that conflicted leaves it.
        std::fs::write(
            local.join(".git").join("MERGE_HEAD"),
            "0000000000000000000000000000000000000000\n",
        )
        .unwrap();

        let outcome = run_preflight_refusals(&local, "test_pipeline").await;
        assert!(
            matches!(
                outcome,
                Err(UpdatePipelineError::MergeInProgress {
                            ..
                })
            ),
            "a wedged clone must reopen on the merge, not be told about the dirt the \
             merge itself created; got {}",
            describe(&outcome)
        );
    }


    /// Name an outcome for a panic message without needing `Debug` on the enum
    /// (its payload variants carry rendered JSON, which would swamp the line).
    fn describe<T>(outcome: &Result<T, UpdatePipelineError>) -> &'static str {
        match outcome {
            Ok(_) => "Ok",
            Err(UpdatePipelineError::MergeInProgress { .. }) => "Err(MergeInProgress)",
            Err(UpdatePipelineError::Conflict { .. }) => "Err(Conflict)",
            Err(UpdatePipelineError::AutostashPopConflict { .. }) => "Err(AutostashPopConflict)",
            Err(UpdatePipelineError::UntrackedCollision { .. }) => "Err(UntrackedCollision)",
            Err(UpdatePipelineError::NonFastForward { .. }) => "Err(NonFastForward)",
            Err(UpdatePipelineError::HeadDidNotAdvance { .. }) => "Err(HeadDidNotAdvance)",
            Err(UpdatePipelineError::Raw(_)) => "Err(Raw)",
        }
    }

    /// The "Already up to date" short-circuit is a SEPARATE outcome, not an
    /// error: the caller returns success without running install.py. Pins that
    /// the extraction kept the branch (ledger step 28) and that it reports
    /// itself through the outcome rather than through a message string the
    /// caller would have to re-parse.
    #[tokio::test]
    async fn pull_sequence_reports_already_up_to_date_without_touching_the_tree() {
        if git_missing() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let (_tmp, _remote, local) = init_repo_pair();

        let outcome = reconcile_and_pull(&local, "main".to_string(), &test_ctx())
            .await
            .unwrap_or_else(|_| panic!("a clone already at the upstream tip must not error"));

        assert!(
            outcome.already_up_to_date,
            "a clone already at the upstream tip must short-circuit"
        );
        assert!(
            outcome
                .db_audit
                .iter()
                .any(|(op, detail)| op == "test_pipeline_complete"
                    && detail.get("note").and_then(|n| n.as_str()) == Some("already_up_to_date")),
            "the no-op path still COLLECTS its completion audit row for the \
             caller to write; rows: {:?}",
            outcome.db_audit.iter().map(|(o, _)| o).collect::<Vec<_>>()
        );
    }

    /// WI-2, as a BEHAVIOURAL assertion rather than a source scan.
    ///
    /// The RC-2 dead end: pre-v0.2.91 the "Already up to date" branch returned
    /// before any staging, so an install whose SOURCE was current but whose
    /// dist binary was stale (the field case: a hand-copied exe after a failed
    /// swap) had no path back to a fresh binary — every later update said
    /// "Already up to date" and changed nothing, forever.
    ///
    /// `tests/test_v0291_binary_delivery_chain.py` pins the SIBLING leg
    /// (`update_run::finish_recovery_git_op`, the Merge/Rebase/Resume
    /// up-to-date result) by source scan. This branch IS reachable, so it gets
    /// the real thing: a clone at the upstream tip whose dist sidecar claims a
    /// version NEWER than the running binary must come back reporting the
    /// staleness. Only an actual `reconcile_dist_at_rest` call can produce
    /// that — a symbol present in a comment cannot.
    #[tokio::test]
    async fn already_up_to_date_still_probes_the_dist_binary_for_staleness() {
        if git_missing() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let (_tmp, _remote, local) = init_repo_pair();

        // A dist sidecar claiming a version far NEWER than this build: the
        // at-rest probe reads it via `read_on_disk_binary_version` and
        // `decide_binary_freshness` returns Stale(OnDiskNewerThanRunning).
        let dist = local
            .join("launcher")
            .join("dist")
            .join(crate::commands::installer::launcher_dist_subdir());
        std::fs::create_dir_all(&dist).unwrap();
        std::fs::write(
            dist.join(format!(
                "{}.metadata.json",
                crate::commands::installer::launcher_binary_filename()
            )),
            r#"{"launcher_version":"99.0.0"}"#,
        )
        .unwrap();

        let outcome = reconcile_and_pull(&local, "main".to_string(), &test_ctx())
            .await
            .unwrap_or_else(|_| panic!("a clone already at the upstream tip must not error"));

        assert!(outcome.already_up_to_date, "the fixture is at the tip");
        assert!(
            outcome.dist_binary_stale,
            "the no-op branch must still PROBE the at-rest dist binary — a sidecar \
             claiming 99.0.0 against this build is stale by any reading. (If this \
             fails with a real update in flight on this machine, the at-rest \
             reconcile stood down by design; re-run at rest.)"
        );
        assert!(
            outcome.db_audit.iter().any(|(op, detail)| op
                == "test_pipeline_complete"
                && detail.get("binary_stale").and_then(|b| b.as_bool()) == Some(true)),
            "and the staleness reaches the audit row the caller writes; rows: {:?}",
            outcome.db_audit
        );
    }

    // ─── v0.2.96 WP-8 (register issue 4): the stall watchdog ──────────────
    //
    // The GUI surface itself (`emit_progress` into a tauri `Window`) is not
    // constructible in unit tests, so the loop is tested through an injected
    // emit collector; the production closure is 4 lines over the SAME
    // `emit_progress(window, "install", …, 50.0)` channel the [VCO-EVENT]
    // parser uses.

    #[test]
    fn stall_window_env_resolution_default_zero_garbage_and_floor() {
        assert_eq!(resolve_stall_warn_secs(None), 600, "documented default");
        assert_eq!(resolve_stall_warn_secs(Some("0")), 0, "0 disables");
        assert_eq!(
            resolve_stall_warn_secs(Some("garbage")),
            600,
            "a typo must fall back to the default, never disable the watchdog"
        );
        assert_eq!(
            resolve_stall_warn_secs(Some("  120  ")),
            120,
            "surrounding whitespace is tolerated"
        );
        assert_eq!(
            resolve_stall_warn_secs(Some("5")),
            30,
            "below the floor is clamped up, not taken literally"
        );
        assert_eq!(
            resolve_stall_warn_secs(Some("-1")),
            600,
            "negative is garbage, not a disable"
        );
    }

    #[test]
    fn stall_state_notices_once_per_window_and_resets_on_progress() {
        let stall = std::time::Duration::from_millis(100);
        let mut st = StallWatchdogState::new(stall);
        let t0 = std::time::Instant::now();
        assert!(!st.notice_due(t0), "fresh state is not stalled");
        assert!(
            !st.notice_due(t0 + std::time::Duration::from_millis(99)),
            "one tick before the window"
        );
        assert!(
            st.notice_due(t0 + std::time::Duration::from_millis(101)),
            "silence past the window fires"
        );
        assert!(
            !st.notice_due(t0 + std::time::Duration::from_millis(150)),
            "no second notice inside the SAME stall window"
        );
        assert!(
            st.notice_due(t0 + std::time::Duration::from_millis(210)),
            "continued silence re-notifies after a further window"
        );
        // Progress resets the silence clock…
        st.note_progress();
        assert!(
            !st.notice_due(t0 + std::time::Duration::from_millis(215)),
            "just after a heartbeat"
        );
        assert!(
            st.notice_due(t0 + std::time::Duration::from_millis(330)),
            "and the window restarts from the heartbeat, not from the notice"
        );
    }

    #[tokio::test]
    async fn stall_watchdog_loop_fires_on_silence_and_is_paced() {
        let stall = std::time::Duration::from_millis(80);
        let tick = std::time::Duration::from_millis(10);
        let state = std::sync::Arc::new(std::sync::Mutex::new(StallWatchdogState::new(stall)));
        let done = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let notices = std::sync::Arc::new(std::sync::Mutex::new(Vec::<String>::new()));

        let watcher = {
            let state = std::sync::Arc::clone(&state);
            let done = std::sync::Arc::clone(&done);
            let sink = std::sync::Arc::clone(&notices);
            tokio::spawn(async move {
                stall_watchdog_loop(stall, tick, state, done, move |msg| {
                    sink.lock().unwrap_or_else(|p| p.into_inner()).push(msg);
                })
                .await;
            })
        };

        let started = std::time::Instant::now();
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        done.store(true, std::sync::atomic::Ordering::Relaxed);
        tokio::time::timeout(std::time::Duration::from_secs(5), watcher)
            .await
            .expect("the watchdog must exit within one tick of done")
            .expect("watchdog join failed");

        let elapsed = started.elapsed();
        let got = notices.lock().unwrap_or_else(|p| p.into_inner()).clone();
        assert!(
            !got.is_empty(),
            "total silence must surface at least one notice"
        );
        let max_notices =
            (elapsed.as_millis() as f64 / stall.as_millis() as f64).floor() + 1.0;
        assert!(
            got.len() as f64 <= max_notices,
            "at most one notice per stall window of continued silence: {} notices \
             in {:?} (bound {})",
            got.len(),
            elapsed,
            max_notices
        );
        assert!(
            got[0].contains("update may be stalled"),
            "message names the state: {}",
            got[0]
        );
        assert!(
            got[0].contains("nothing was cancelled"),
            "message must not read as an abort: {}",
            got[0]
        );
        assert!(
            got[0].len() < 200,
            "message must honour the GUI detail-length contract (progress.rs): {}",
            got[0]
        );
    }

    #[tokio::test]
    async fn stall_watchdog_is_silent_while_progress_keeps_flowing() {
        let stall = std::time::Duration::from_millis(80);
        let tick = std::time::Duration::from_millis(10);
        let state = std::sync::Arc::new(std::sync::Mutex::new(StallWatchdogState::new(stall)));
        let done = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let notices = std::sync::Arc::new(std::sync::Mutex::new(Vec::<String>::new()));

        // A heartbeat every 30 ms — always inside the 80 ms stall window.
        let feeder = {
            let state = std::sync::Arc::clone(&state);
            let done = std::sync::Arc::clone(&done);
            tokio::spawn(async move {
                for _ in 0..8 {
                    tokio::time::sleep(std::time::Duration::from_millis(30)).await;
                    state
                        .lock()
                        .unwrap_or_else(|p| p.into_inner())
                        .note_progress();
                }
                done.store(true, std::sync::atomic::Ordering::Relaxed);
            })
        };
        let watcher = {
            let sink = std::sync::Arc::clone(&notices);
            let state = std::sync::Arc::clone(&state);
            let done = std::sync::Arc::clone(&done);
            tokio::spawn(async move {
                stall_watchdog_loop(stall, tick, state, done, move |msg| {
                    sink.lock().unwrap_or_else(|p| p.into_inner()).push(msg);
                })
                .await;
            })
        };
        let _ = tokio::time::timeout(std::time::Duration::from_secs(5), feeder).await;
        tokio::time::timeout(std::time::Duration::from_secs(5), watcher)
            .await
            .expect("watchdog must exit once the feeder sets done")
            .expect("watchdog join failed");

        let got = notices.lock().unwrap_or_else(|p| p.into_inner());
        assert!(
            got.is_empty(),
            "a stdout heartbeat well inside the stall window must never fire a \
             stall notice; got {:?}",
            got
        );
    }

    #[tokio::test]
    async fn stall_watchdog_exits_promptly_once_done() {
        let stall = std::time::Duration::from_secs(600);
        let tick = std::time::Duration::from_millis(10);
        let state = std::sync::Arc::new(std::sync::Mutex::new(StallWatchdogState::new(stall)));
        let done = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let sink = std::sync::Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let notices = std::sync::Arc::clone(&sink);

        // `done` pre-set: the very first post-tick check must return — the
        // loop may never outlive the reader it watches by more than a tick.
        tokio::time::timeout(
            std::time::Duration::from_secs(5),
            stall_watchdog_loop(stall, tick, state, done, move |msg| {
                notices.lock().unwrap_or_else(|p| p.into_inner()).push(msg);
            }),
        )
        .await
        .expect("pre-set done must terminate the loop within one tick");
        assert!(
            sink.lock().unwrap_or_else(|p| p.into_inner()).is_empty(),
            "a done watchdog must not fire on its way out"
        );
    }
}
