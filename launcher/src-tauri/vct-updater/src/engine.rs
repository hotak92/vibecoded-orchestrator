// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// The cross-OS UPDATE ENGINE (DESIGN-v0300-update-system-architecture.md §3.1
// "DESTINATION UPDATE ENGINE").
//
// ============================================================================
// DORMANT in v0.2.60.
// ============================================================================
// NOTHING in v0.2.60 invokes this. `main()` (main.rs) is the LIVE entrypoint
// and it does ONLY the swap-only path (parse update.lock.json → wait → swap →
// relaunch). The engine here is the v0.3.0 inverted-updater body: a separate
// process, spawned by the thin bootstrap stub from the DESTINATION (post-fetch)
// build, that owns the WHOLE update — pull → install → migrate → swap →
// relaunch — running AFTER the launcher has exited (so it holds no
// launcher.db lock; this dissolves the v0.2.60 Windows DB-lock bug per
// DESIGN §1.4 / R6).
//
// The engine entrypoint (`run_engine`) is NOT wired into `main()` — there is
// no `EngineArgs::from_argv` dispatch in main.rs (proven by
// `engine_entrypoint_is_not_wired_into_main`). v0.3.0 adds that dispatch
// (`vct-updater --engine <plan.json>`); until then the engine exists only to
// be unit-tested and audited.
//
// ============================================================================
// PER-OS SWAP DISPATCH — THE LOAD-BEARING CONSTRAINT (read before auditing).
// ============================================================================
// The engine UNIFIES the orchestration (the pull→install→migrate→relaunch
// sequence is identical on every OS) but DISPATCHES the SWAP step per-OS,
// because the binary-swap mechanism is fundamentally different by OS and that
// difference is CORRECT (knowledge/concepts/binary-swap-per-os-strategy-...md):
//
//   * POSIX swap  = NO-OP. `git pull` (the `pull` step) already overwrote the
//     on-disk binary IN PLACE; the kernel ref-counts the running inode. There
//     is no discrete swap. The relaunch is a re-exec of the freshly-
//     overwritten on-disk binary (the launcher's restart_launcher /
//     WaitForBinaryRefresh semantics). `dispatch_swap` therefore returns
//     `SwapDispatch::PosixNoOp` and does NO MoveFileEx / `.new` ceremony.
//     Imposing the Windows ceremony on POSIX would be a regression — the
//     `posix_swap_is_a_noop` test guards exactly this.
//
//   * Windows swap = the EXISTING `vct-updater` mechanism: wait for the
//     parent PID to exit (mandatory-lock release), then
//     `MoveFileEx(<target>.new → <target>)` per swap entry, via the SAME
//     `swap::swap_binary` / `swap::wait_for_parent_exit` the live `main()`
//     calls (reuse, not reimplement). `dispatch_swap` returns
//     `SwapDispatch::WindowsMoveFileEx` and performs it.
//
// The orchestration AROUND the swap (the new unified part) is `run_plan`.

// DORMANT in v0.2.60: the engine has NO live caller (no `--engine` dispatch
// in main.rs — see `engine_entrypoint_is_not_wired_into_main`). dead_code is
// intentional until v0.3.0 wires the inverted entrypoint.
#![allow(dead_code)]

use std::path::Path;
use std::process::Command;

use crate::swap;
use crate::update_plan::UpdatePlan;

/// How the per-OS swap step resolved for a plan. The variant itself encodes
/// the per-OS strategy so a test can assert "POSIX never takes the Windows
/// MoveFileEx ceremony" without running a real swap.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SwapDispatch {
    /// POSIX: git pull overwrote in place; the swap is a no-op. Relaunch is a
    /// re-exec of the on-disk binary. NO `.new` / MoveFileEx ceremony.
    PosixNoOp,
    /// Windows: wait for parent + MoveFileEx the staged `<target>.new` files.
    /// `swaps_attempted` / `swap_failures` mirror the live `main()` accounting.
    WindowsMoveFileEx {
        swaps_attempted: usize,
        swap_failures: usize,
    },
}

/// Outcome of running a step. Soft-fail granular so the engine can decide
/// whether to gate the swap (migration failure ⇒ skip swap, relaunch OLD
/// binary — DESIGN §5.3 "binary swap gated on migration success").
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StepOutcome {
    pub step: String,
    pub ok: bool,
    pub skipped: bool,
    pub detail: String,
}

/// Aggregate result of running an `UpdatePlan` through the engine.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EngineRunResult {
    pub steps: Vec<StepOutcome>,
    /// The per-OS swap dispatch decision (None when the swap step was gated
    /// out, e.g. a prior migration failure).
    pub swap_dispatch: Option<SwapDispatch>,
    /// True iff every requested non-skipped step succeeded.
    pub ok: bool,
}

/// A pluggable runner so tests can drive `run_plan` without spawning real
/// git / python / MoveFileEx. The production `RealRunner` shells out; tests
/// inject a scripted runner.
pub trait StepRunner {
    /// Run `git pull` (or fetch+merge) of the destination clone. Returns
    /// Ok(detail) on success.
    fn pull(&self, plan: &UpdatePlan) -> Result<String, String>;
    /// Run `install.py --update` from the post-pull source.
    fn install(&self, plan: &UpdatePlan) -> Result<String, String>;
    /// Run the bundled migration set (SQLite + Weaviate) via the Piece-2
    /// runner. DESTRUCTIVE migrations are NOT auto-applied (the runner emits
    /// deferrals) — the engine never forces a re-embed.
    fn migrate(&self, plan: &UpdatePlan) -> Result<String, String>;
    /// Perform the per-OS swap. Returns the dispatch decision.
    fn swap(&self, plan: &UpdatePlan) -> Result<SwapDispatch, String>;
    /// Relaunch the launcher. POSIX = re-exec on-disk binary; Windows =
    /// spawn the swapped binary detached.
    fn relaunch(&self, plan: &UpdatePlan) -> Result<String, String>;
}

/// Run an `UpdatePlan` through the engine: the UNIFIED orchestration with a
/// PER-OS swap dispatch. Ordering + gating per DESIGN §5.3:
///   pull → install → migrate → (swap GATED on migrate success) → relaunch.
/// A failed step before the swap aborts the swap (the OLD binary stays
/// runnable) and the engine relaunches the OLD launcher so the user is told
/// what failed — never a half-swapped engine + un-migrated data.
pub fn run_plan<R: StepRunner>(plan: &UpdatePlan, runner: &R) -> EngineRunResult {
    let mut steps: Vec<StepOutcome> = Vec::new();
    let mut swap_dispatch: Option<SwapDispatch> = None;

    // Refuse a plan whose schema major this engine predates (DESIGN R1).
    if !plan.schema_is_honorable() {
        steps.push(StepOutcome {
            step: "schema_check".into(),
            ok: false,
            skipped: false,
            detail: format!(
                "plan_schema {} exceeds this engine's max {} — refusing to \
                 mis-execute a future plan",
                plan.plan_schema,
                crate::update_plan::PLAN_SCHEMA_MAJOR
            ),
        });
        return EngineRunResult {
            steps,
            swap_dispatch,
            ok: false,
        };
    }

    // Helper: record + return whether to keep going.
    macro_rules! run_step {
        ($name:literal, $call:expr) => {{
            if plan.wants_step($name) {
                match $call {
                    Ok(detail) => {
                        steps.push(StepOutcome {
                            step: $name.into(),
                            ok: true,
                            skipped: false,
                            detail,
                        });
                        true
                    }
                    Err(e) => {
                        steps.push(StepOutcome {
                            step: $name.into(),
                            ok: false,
                            skipped: false,
                            detail: e,
                        });
                        false
                    }
                }
            } else {
                steps.push(StepOutcome {
                    step: $name.into(),
                    ok: true,
                    skipped: true,
                    detail: "not requested in plan.steps".into(),
                });
                true
            }
        }};
    }

    // pull → install → migrate, each gating the next.
    let mut healthy = run_step!("pull", runner.pull(plan));
    if healthy {
        healthy = run_step!("install", runner.install(plan));
    }
    if healthy {
        healthy = run_step!("migrate", runner.migrate(plan));
    }

    // SWAP is GATED on the pre-swap steps succeeding (DESIGN §5.3). If
    // anything failed, we do NOT swap — the OLD binary stays runnable — but
    // we STILL relaunch (the OLD launcher) so the failure surfaces.
    if healthy && plan.wants_step("swap") {
        // The swap step's recorded `ok` feeds the final `ok` computation
        // (which inspects `steps`); `healthy` was only the GATE for whether
        // to attempt the swap at all, so it is not re-assigned here.
        match runner.swap(plan) {
            Ok(dispatch) => {
                let ok = match &dispatch {
                    SwapDispatch::PosixNoOp => true,
                    SwapDispatch::WindowsMoveFileEx { swap_failures, .. } => *swap_failures == 0,
                };
                swap_dispatch = Some(dispatch.clone());
                steps.push(StepOutcome {
                    step: "swap".into(),
                    ok,
                    skipped: false,
                    detail: format!("{:?}", dispatch),
                });
            }
            Err(e) => {
                steps.push(StepOutcome {
                    step: "swap".into(),
                    ok: false,
                    skipped: false,
                    detail: e,
                });
            }
        }
    } else if plan.wants_step("swap") {
        // Gated out by a prior failure.
        steps.push(StepOutcome {
            step: "swap".into(),
            ok: true,
            skipped: true,
            detail: "skipped: a prior step failed — leaving OLD binary in place".into(),
        });
    } else {
        steps.push(StepOutcome {
            step: "swap".into(),
            ok: true,
            skipped: true,
            detail: "not requested in plan.steps".into(),
        });
    }

    // Relaunch ALWAYS runs if requested (even on failure — relaunching the
    // OLD launcher is how the failure toast reaches the user; the alternative
    // — no launcher — hides it). Its success doesn't change `ok`.
    if plan.wants_step("relaunch") {
        match runner.relaunch(plan) {
            Ok(detail) => steps.push(StepOutcome {
                step: "relaunch".into(),
                ok: true,
                skipped: false,
                detail,
            }),
            Err(e) => steps.push(StepOutcome {
                step: "relaunch".into(),
                ok: false,
                skipped: false,
                detail: e,
            }),
        }
    }

    // `ok` = every non-skipped step before/including swap succeeded.
    let ok = steps
        .iter()
        .filter(|s| !s.skipped && s.step != "relaunch")
        .all(|s| s.ok);
    EngineRunResult {
        steps,
        swap_dispatch,
        ok,
    }
}

/// The PER-OS SWAP DISPATCH — the constraint's heart. Decides + performs the
/// swap appropriate to the host OS, reusing `swap.rs` (shared with `main()`).
///
/// POSIX → `PosixNoOp` (git pull already overwrote in place; NO MoveFileEx).
/// Windows → wait for parent PID, then `swap::swap_binary` per entry.
pub fn dispatch_swap(plan: &UpdatePlan) -> Result<SwapDispatch, String> {
    #[cfg(not(target_os = "windows"))]
    {
        let _ = plan; // POSIX: nothing to do — the binary is already swapped.
        Ok(SwapDispatch::PosixNoOp)
    }

    #[cfg(target_os = "windows")]
    {
        // Wait for the launcher (parent) to exit so its mandatory file lock
        // on the .exe is released — IDENTICAL to the live main() path.
        // AlreadyGone is fine (proceed); Timeout means still-locked (abort).
        match swap::wait_for_parent_exit(plan.parent_pid) {
            Ok(_) | Err(swap::WaitError::AlreadyGone) => {}
            Err(swap::WaitError::Timeout) => {
                return Err(format!(
                    "parent {} did not exit within {}s — binary still locked, aborting swap",
                    plan.parent_pid,
                    swap::PARENT_WAIT_TIMEOUT_SECS
                ));
            }
        }
        let mut attempted = 0usize;
        let mut failures = 0usize;
        for entry in &plan.swaps {
            attempted += 1;
            match swap::swap_binary(&entry.target) {
                Ok(_) => {}
                Err(_) => failures += 1,
            }
        }
        Ok(SwapDispatch::WindowsMoveFileEx {
            swaps_attempted: attempted,
            swap_failures: failures,
        })
    }
}

// ---------------------------------------------------------------------------
// Production StepRunner: shells out to git / install.py / the Piece-2 runner.
// (DORMANT — no live caller; v0.3.0 wires `run_engine` to use it.)
// ---------------------------------------------------------------------------

/// The production runner. Each method shells out from the DESTINATION clone.
pub struct RealRunner;

impl RealRunner {
    fn require<'a>(opt: &'a Option<std::path::PathBuf>, what: &str) -> Result<&'a Path, String> {
        opt.as_deref()
            .ok_or_else(|| format!("plan is missing `{}` (required for the engine)", what))
    }
}

impl StepRunner for RealRunner {
    fn pull(&self, plan: &UpdatePlan) -> Result<String, String> {
        let root = Self::require(&plan.install_root, "install_root")?;
        let remote = plan.upstream_remote.as_deref().unwrap_or("vco_upstream");
        let branch = plan.branch.as_deref().unwrap_or("main");
        // Fetch then ff-only pull from the pinned upstream. The launcher's
        // richer pre-merge gating (installer.rs) is NOT duplicated here — for
        // the inverted path the stub set up the remote + the engine does the
        // minimal pull; v0.3.0 may thread more of the gate in.
        let fetch = Command::new("git")
            .args(["fetch", remote, branch])
            .current_dir(root)
            .output()
            .map_err(|e| format!("git fetch spawn: {}", e))?;
        if !fetch.status.success() {
            return Err(format!(
                "git fetch {} {} failed: {}",
                remote,
                branch,
                String::from_utf8_lossy(&fetch.stderr)
            ));
        }
        let pull = Command::new("git")
            .args(["merge", "--ff-only", &format!("{}/{}", remote, branch)])
            .current_dir(root)
            .output()
            .map_err(|e| format!("git merge spawn: {}", e))?;
        if !pull.status.success() {
            return Err(format!(
                "git merge --ff-only failed: {}",
                String::from_utf8_lossy(&pull.stderr)
            ));
        }
        Ok(format!("pulled {}/{} into {}", remote, branch, root.display()))
    }

    fn install(&self, plan: &UpdatePlan) -> Result<String, String> {
        let root = Self::require(&plan.install_root, "install_root")?;
        let install_py = root.join("install.py");
        if !install_py.is_file() {
            return Err(format!("install.py not found at {}", install_py.display()));
        }
        // Resolve a python interpreter the same way the launcher does is out
        // of scope here; the engine uses `python3`/`python` on PATH (the
        // destination clone's install.py is self-contained re: its venv).
        let py = python_cmd();
        let out = Command::new(&py)
            .arg(install_py.as_os_str())
            .arg("--update")
            .current_dir(root)
            .output()
            .map_err(|e| format!("install.py spawn ({}): {}", py, e))?;
        if !out.status.success() {
            return Err(format!(
                "install.py --update exited {}: {}",
                out.status,
                String::from_utf8_lossy(&out.stderr)
            ));
        }
        Ok("install.py --update ok".into())
    }

    fn migrate(&self, plan: &UpdatePlan) -> Result<String, String> {
        let root = Self::require(&plan.install_root, "install_root")?;
        // REUSE the Piece-2 migration runner (do NOT reimplement) by invoking
        // it the SAME way `hard_cut` / install.py do — via the Python module.
        // Destructive migrations are NOT auto-applied (the runner emits
        // deferrals); the engine never forces a re-embed.
        let py = python_cmd();
        let snippet = "import sys; from pathlib import Path; \
             from vco_lib.schema_migration_runner import run_schema_migrations; \
             run_schema_migrations(db_path=Path(sys.argv[1]), project_id=(sys.argv[2] or None), \
             migrations_dir=Path(sys.argv[3])/'migrations', env=dict(__import__('os').environ), \
             include_orchestrator_wide=True)";
        let vct_root = Self::require(&plan.vct_root, "vct_root")?;
        let db_path = vct_root.join("launcher.db");
        let out = Command::new(&py)
            .arg("-c")
            .arg(snippet)
            .arg(db_path.as_os_str())
            .arg("") // project_id resolved by the runner / install context
            .arg(root.as_os_str())
            .current_dir(root)
            .output()
            .map_err(|e| format!("migration runner spawn ({}): {}", py, e))?;
        if !out.status.success() {
            return Err(format!(
                "migration runner exited {}: {}",
                out.status,
                String::from_utf8_lossy(&out.stderr)
            ));
        }
        Ok("migration runner ok (Piece 2, reused; destructive edges deferred)".into())
    }

    fn swap(&self, plan: &UpdatePlan) -> Result<SwapDispatch, String> {
        dispatch_swap(plan)
    }

    fn relaunch(&self, plan: &UpdatePlan) -> Result<String, String> {
        let exe = Self::require(&plan.relaunch, "relaunch")?;
        if !exe.is_file() {
            return Err(format!("relaunch binary not found: {}", exe.display()));
        }
        swap::spawn_detached(exe)?;
        Ok(format!("relaunched {}", exe.display()))
    }
}

/// Resolve a python interpreter name. Cheap heuristic — the engine runs from
/// the destination clone whose install.py is self-contained; full venv
/// resolution is the launcher's job and out of scope for the dormant engine.
fn python_cmd() -> String {
    if cfg!(target_os = "windows") {
        "python".to_string()
    } else {
        "python3".to_string()
    }
}

/// The engine entrypoint the v0.3.0 stub will spawn:
/// `vct-updater --engine <plan.json>`. DORMANT in v0.2.60 — `main()` never
/// dispatches to it (no `--engine` arg parsing in main.rs). Reads + validates
/// the plan, then `run_plan` with the `RealRunner`.
///
/// NOT marked `#[allow(dead_code)]` carelessly: it is genuinely unreachable in
/// v0.2.60 by design, and the `engine_entrypoint_is_not_wired_into_main` test
/// asserts main.rs contains no call to it.
#[allow(dead_code)]
pub fn run_engine(plan_path: &Path) -> Result<EngineRunResult, String> {
    let content = std::fs::read_to_string(plan_path)
        .map_err(|e| format!("read plan {}: {}", plan_path.display(), e))?;
    let plan: UpdatePlan =
        serde_json::from_str(&content).map_err(|e| format!("parse plan: {}", e))?;
    Ok(run_plan(&plan, &RealRunner))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::update_plan::PlanSwap;
    use std::cell::RefCell;
    use std::path::PathBuf;

    /// A scripted runner that records calls + returns scripted results, so we
    /// can drive `run_plan` without real git/python/MoveFileEx.
    struct ScriptedRunner {
        calls: RefCell<Vec<String>>,
        pull_ok: bool,
        install_ok: bool,
        migrate_ok: bool,
        swap_result: SwapDispatch,
        relaunch_ok: bool,
    }

    impl Default for ScriptedRunner {
        fn default() -> Self {
            ScriptedRunner {
                calls: RefCell::new(Vec::new()),
                pull_ok: true,
                install_ok: true,
                migrate_ok: true,
                swap_result: SwapDispatch::PosixNoOp,
                relaunch_ok: true,
            }
        }
    }

    impl StepRunner for ScriptedRunner {
        fn pull(&self, _: &UpdatePlan) -> Result<String, String> {
            self.calls.borrow_mut().push("pull".into());
            if self.pull_ok {
                Ok("pull ok".into())
            } else {
                Err("pull failed".into())
            }
        }
        fn install(&self, _: &UpdatePlan) -> Result<String, String> {
            self.calls.borrow_mut().push("install".into());
            if self.install_ok {
                Ok("install ok".into())
            } else {
                Err("install failed".into())
            }
        }
        fn migrate(&self, _: &UpdatePlan) -> Result<String, String> {
            self.calls.borrow_mut().push("migrate".into());
            if self.migrate_ok {
                Ok("migrate ok".into())
            } else {
                Err("migrate failed".into())
            }
        }
        fn swap(&self, _: &UpdatePlan) -> Result<SwapDispatch, String> {
            self.calls.borrow_mut().push("swap".into());
            Ok(self.swap_result.clone())
        }
        fn relaunch(&self, _: &UpdatePlan) -> Result<String, String> {
            self.calls.borrow_mut().push("relaunch".into());
            if self.relaunch_ok {
                Ok("relaunch ok".into())
            } else {
                Err("relaunch failed".into())
            }
        }
    }

    fn full_plan() -> UpdatePlan {
        let mut p = UpdatePlan::swap_only(
            123,
            vec![PlanSwap {
                target: PathBuf::from("/x/vct-launcher"),
            }],
            Some(PathBuf::from("/x/vct-launcher")),
        );
        p.install_root = Some(PathBuf::from("/x/clone"));
        p.vct_root = Some(PathBuf::from("/x/.vct"));
        p.steps = vec![
            "pull".into(),
            "install".into(),
            "migrate".into(),
            "swap".into(),
            "relaunch".into(),
        ];
        p
    }

    #[test]
    fn full_plan_runs_all_steps_in_order() {
        let plan = full_plan();
        let runner = ScriptedRunner::default();
        let result = run_plan(&plan, &runner);
        assert!(result.ok, "all steps succeeded → ok");
        assert_eq!(
            *runner.calls.borrow(),
            vec!["pull", "install", "migrate", "swap", "relaunch"]
        );
    }

    // GATING: a migration FAILURE must skip the swap (leave OLD binary) but
    // STILL relaunch the OLD launcher so the failure surfaces.
    #[test]
    fn migration_failure_gates_the_swap() {
        let plan = full_plan();
        let runner = ScriptedRunner {
            migrate_ok: false,
            ..Default::default()
        };
        let result = run_plan(&plan, &runner);
        assert!(!result.ok, "migration failure → not ok");
        // swap was NOT performed.
        assert!(
            !runner.calls.borrow().contains(&"swap".to_string()),
            "swap must be skipped after a migration failure"
        );
        // relaunch STILL happened (surface the failure).
        assert!(runner.calls.borrow().contains(&"relaunch".to_string()));
        // swap step recorded as skipped.
        let swap_step = result.steps.iter().find(|s| s.step == "swap").unwrap();
        assert!(swap_step.skipped);
    }

    // A swap FAILURE (Windows MoveFileEx failure) → not ok, but relaunch still
    // runs (the OLD binary is the runnable one — relaunch surfaces the toast).
    #[test]
    fn windows_swap_failure_is_not_ok_but_relaunches() {
        let plan = full_plan();
        let runner = ScriptedRunner {
            swap_result: SwapDispatch::WindowsMoveFileEx {
                swaps_attempted: 2,
                swap_failures: 1,
            },
            ..Default::default()
        };
        let result = run_plan(&plan, &runner);
        assert!(!result.ok, "a swap failure → not ok");
        assert!(runner.calls.borrow().contains(&"relaunch".to_string()));
    }

    // A swap-only plan (legacy lock shape) runs ONLY swap + relaunch — no
    // pull/install/migrate.
    #[test]
    fn swap_only_plan_skips_pull_install_migrate() {
        let plan = UpdatePlan::swap_only(
            1,
            vec![],
            Some(PathBuf::from("/x/vct-launcher")),
        );
        let runner = ScriptedRunner::default();
        let result = run_plan(&plan, &runner);
        assert!(result.ok);
        let calls = runner.calls.borrow();
        assert!(!calls.contains(&"pull".to_string()));
        assert!(!calls.contains(&"install".to_string()));
        assert!(!calls.contains(&"migrate".to_string()));
        assert!(calls.contains(&"swap".to_string()));
        assert!(calls.contains(&"relaunch".to_string()));
    }

    // A future-schema plan is refused before any step runs.
    #[test]
    fn future_schema_plan_refused() {
        let mut plan = full_plan();
        plan.plan_schema = crate::update_plan::PLAN_SCHEMA_MAJOR + 1;
        let runner = ScriptedRunner::default();
        let result = run_plan(&plan, &runner);
        assert!(!result.ok);
        assert!(runner.calls.borrow().is_empty(), "no step runs on a refused plan");
    }

    // PER-OS SWAP DISPATCH — the load-bearing constraint. On POSIX (the test
    // host), `dispatch_swap` MUST return PosixNoOp — it must NEVER take the
    // Windows MoveFileEx path. This is the structural guard against the
    // regression "impose the Windows .new/MoveFileEx ceremony on POSIX".
    #[cfg(not(target_os = "windows"))]
    #[test]
    fn posix_swap_is_a_noop() {
        let plan = full_plan();
        let dispatch = dispatch_swap(&plan).unwrap();
        assert_eq!(
            dispatch,
            SwapDispatch::PosixNoOp,
            "POSIX swap MUST be a no-op (git pull overwrote in place); the \
             Windows MoveFileEx ceremony must NOT run on POSIX"
        );
    }

    // The Windows dispatch variant is structurally distinct from the POSIX
    // one — asserting the type-level separation regardless of test host.
    #[test]
    fn dispatch_variants_are_distinct_per_os() {
        let posix = SwapDispatch::PosixNoOp;
        let windows = SwapDispatch::WindowsMoveFileEx {
            swaps_attempted: 1,
            swap_failures: 0,
        };
        assert_ne!(posix, windows);
    }
}
