// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// `update.plan.json` — the v2 handoff contract for the v0.3.0 inverted
// updater (DESIGN-v0300-update-system-architecture.md §3.3).
//
// ============================================================================
// DORMANT in v0.2.60.
// ============================================================================
// This module is COMPILED but never executed by any live v0.2.60 code path.
// The LIVE Windows swap (vct-updater's `main()` in main.rs) still parses the
// legacy `update.lock.json` via the `UpdateLock` struct — that path is
// unchanged. `update.plan.json` is the SUPERSET schema that the v0.3.0
// inverted stub→engine handoff will use; it is exercised in v0.2.60 only by
// unit tests + the (not-wired) engine/bootstrap entrypoints.
//
// Forever-stable contract (DESIGN §3.2, §3.3, R1):
//   * `plan_schema` is a frozen MAJOR. Within a major, fields are
//     append-only / optional-additive ONLY — every field after the required
//     core carries `#[serde(default)]`. A v0.3.0 stub's plan MUST be parseable
//     by a v0.9.0 engine and vice-versa.
//   * `UpdatePlan` is a strict SUPERSET of the legacy `UpdateLock`
//     (main.rs / update_handoff.rs): `parent_pid`, `swaps`, `relaunch`,
//     `started_at` carry identical meaning, so an `UpdatePlan` JSON also
//     parses as an `UpdateLock` (the swap-only fields), preserving the
//     existing Windows swap contract byte-for-byte.

// DORMANT in v0.2.60: every item here is exercised only by unit tests + the
// (not-wired) engine/bootstrap. No LIVE v0.2.60 path constructs an UpdatePlan,
// so `dead_code` is expected and intentional until v0.3.0 wires the inverted
// entrypoint. The `engine_entrypoint_is_not_wired_into_main` test is the
// enforced dormancy guarantee.
#![allow(dead_code)]

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

/// The frozen schema major the engine/bootstrap in THIS build understands.
/// An engine that reads a plan with a higher major MUST refuse rather than
/// mis-execute (DESIGN R1). Bumped only on a breaking wire change (which the
/// `#[serde(default)]`-everywhere discipline is designed to avoid).
pub const PLAN_SCHEMA_MAJOR: u32 = 1;

/// A single binary swap entry. Identical shape + meaning to
/// `main.rs::SwapEntry` and `update_handoff.rs::SwapEntry` — the engine's
/// Windows swap step reuses the exact same `<target>.new → <target>`
/// convention.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanSwap {
    /// Canonical absolute path of the binary to overwrite. The Windows swap
    /// looks for `<target>.new` and renames it to `<target>`.
    pub target: PathBuf,
}

/// The v0.3.0 handoff plan. SUPERSET of the legacy `UpdateLock`: the first
/// four fields (`parent_pid`, `swaps`, `relaunch`, `started_at`) are the
/// legacy contract verbatim; everything else is `#[serde(default)]` so an
/// old engine parsing a new plan (or a new engine parsing an old lock)
/// degrades gracefully instead of failing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UpdatePlan {
    // ── Legacy `UpdateLock` core (kept byte-compatible) ──────────────────
    /// PID of the launcher/stub that requested this update. The engine
    /// waits for this PID to exit before performing any swap (Windows) or
    /// re-exec (POSIX) — exactly as the legacy swap does.
    pub parent_pid: u32,

    /// Binaries to swap. On Windows each `<target>.new` is renamed to
    /// `<target>` (MoveFileEx). On POSIX the binaries are already in place
    /// (git pull overwrote them) — the swap step is a no-op (see
    /// `engine::dispatch_swap`).
    pub swaps: Vec<PlanSwap>,

    /// Path to spawn after the swap completes (the new launcher). Optional
    /// for parity with `UpdateLock`.
    #[serde(default)]
    pub relaunch: Option<PathBuf>,

    /// ISO-8601 timestamp the stub set when writing the plan. Used by the
    /// boot-time staleness check, identical to `UpdateLock::started_at`.
    #[serde(default)]
    pub started_at: Option<String>,

    // ── v2 additive fields (DORMANT; default-everything) ─────────────────
    /// Frozen schema major. Absent (legacy `update.lock.json`) → treated as
    /// `0` (= "this is a legacy lock, swap-only"). A plan that declares a
    /// major HIGHER than `PLAN_SCHEMA_MAJOR` is refused by the engine.
    #[serde(default)]
    pub plan_schema: u32,

    /// Orchestrator clone root (the CODE; what the engine's `pull`/`install`
    /// steps operate on).
    #[serde(default)]
    pub install_root: Option<PathBuf>,

    /// `~/.vct` — where the gate/lock/result/backup files live.
    #[serde(default)]
    pub vct_root: Option<PathBuf>,

    /// Currently-installed version (from `_read_install_version`). Used by the
    /// floor check (Piece 5; checked by the stub, mirrored here for the log).
    #[serde(default)]
    pub from_version: Option<String>,

    /// Target version (from the fetched release metadata).
    #[serde(default)]
    pub to_version: Option<String>,

    /// Pinned public upstream remote (the stub ran `ensure_upstream_remote`).
    #[serde(default)]
    pub upstream_remote: Option<String>,

    /// Branch to pull (`main` unless overridden).
    #[serde(default)]
    pub branch: Option<String>,

    /// The verified engine binary the stub spawned (forensic; the engine is
    /// already running by the time it reads this).
    #[serde(default)]
    pub engine_binary: Option<PathBuf>,

    /// The sha256 the stub verified `engine_binary` against BEFORE spawning
    /// it (forensic record of what was trusted).
    #[serde(default)]
    pub engine_sha256: Option<String>,

    /// Ordered step list the engine should run. Empty → swap-only (legacy
    /// behaviour). Full inverted set: `["pull","install","migrate","swap","relaunch"]`.
    #[serde(default)]
    pub steps: Vec<String>,

    /// True when the floor check (Piece 5) routed this to the guided hard-cut
    /// instead of an in-place pull. DORMANT (the floor is `0.0.0` in v0.2.60).
    #[serde(default)]
    pub hard_cut: bool,
}

impl UpdatePlan {
    /// Construct a minimal swap-only plan (the legacy `UpdateLock` shape,
    /// expressed as a plan). Used where the new schema is wanted but the
    /// behaviour must stay swap-only.
    pub fn swap_only(parent_pid: u32, swaps: Vec<PlanSwap>, relaunch: Option<PathBuf>) -> Self {
        UpdatePlan {
            parent_pid,
            swaps,
            relaunch,
            started_at: None,
            plan_schema: PLAN_SCHEMA_MAJOR,
            install_root: None,
            vct_root: None,
            from_version: None,
            to_version: None,
            upstream_remote: None,
            branch: None,
            engine_binary: None,
            engine_sha256: None,
            steps: Vec::new(),
            hard_cut: false,
        }
    }

    /// True iff the engine in THIS build can honor the plan's schema major.
    /// A plan whose `plan_schema` exceeds `PLAN_SCHEMA_MAJOR` is from a future
    /// release the running engine predates — refuse rather than mis-execute
    /// (DESIGN R1). A legacy lock (`plan_schema == 0`) is always honorable
    /// (swap-only), since 0 < PLAN_SCHEMA_MAJOR.
    pub fn schema_is_honorable(&self) -> bool {
        self.plan_schema <= PLAN_SCHEMA_MAJOR
    }

    /// The ordered steps the engine should run, normalized. An empty `steps`
    /// list (legacy lock, or a plan that didn't specify) means swap-only:
    /// the engine runs ONLY the per-OS swap + relaunch and skips
    /// pull/install/migrate. This keeps a plan-shaped legacy lock behaving
    /// exactly like today's swap.
    pub fn effective_steps(&self) -> Vec<String> {
        if self.steps.is_empty() {
            vec!["swap".to_string(), "relaunch".to_string()]
        } else {
            self.steps.clone()
        }
    }

    pub fn wants_step(&self, step: &str) -> bool {
        self.effective_steps().iter().any(|s| s == step)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plan_roundtrips() {
        let plan = UpdatePlan {
            parent_pid: 4242,
            swaps: vec![PlanSwap {
                target: PathBuf::from("/x/vct-launcher"),
            }],
            relaunch: Some(PathBuf::from("/x/vct-launcher")),
            started_at: Some("2026-06-16T00:00:00Z".to_string()),
            plan_schema: PLAN_SCHEMA_MAJOR,
            install_root: Some(PathBuf::from("/home/u/clone")),
            vct_root: Some(PathBuf::from("/home/u/.vct")),
            from_version: Some("0.2.60".to_string()),
            to_version: Some("0.3.0".to_string()),
            upstream_remote: Some("vco_upstream".to_string()),
            branch: Some("main".to_string()),
            engine_binary: Some(PathBuf::from("/home/u/.vct/update-staging/vct-updater")),
            engine_sha256: Some("deadbeef".to_string()),
            steps: vec![
                "pull".into(),
                "install".into(),
                "migrate".into(),
                "swap".into(),
                "relaunch".into(),
            ],
            hard_cut: false,
        };
        let json = serde_json::to_string(&plan).unwrap();
        let back: UpdatePlan = serde_json::from_str(&json).unwrap();
        assert_eq!(plan, back);
    }

    // The forever-stable contract: a LEGACY `update.lock.json` (the swap-only
    // wire today's main.rs writes) parses cleanly as an `UpdatePlan` —
    // proving the superset relationship holds and the engine can consume a
    // legacy lock as a swap-only plan.
    #[test]
    fn legacy_lock_parses_as_plan_swap_only() {
        let legacy = r#"{
            "parent_pid": 1234,
            "swaps": [{"target": "C:\\x\\vct-launcher.exe"}],
            "relaunch": "C:\\x\\vct-launcher.exe",
            "started_at": "2026-06-09T18:30:00Z"
        }"#;
        let plan: UpdatePlan = serde_json::from_str(legacy).unwrap();
        assert_eq!(plan.parent_pid, 1234);
        assert_eq!(plan.swaps.len(), 1);
        // Absent v2 fields default cleanly.
        assert_eq!(plan.plan_schema, 0, "legacy lock has no plan_schema → 0");
        assert!(plan.steps.is_empty());
        assert!(!plan.hard_cut);
        // A schema-0 (legacy) plan is honorable (swap-only) and its
        // effective steps degrade to swap+relaunch.
        assert!(plan.schema_is_honorable());
        assert_eq!(plan.effective_steps(), vec!["swap", "relaunch"]);
        assert!(plan.wants_step("swap"));
        assert!(!plan.wants_step("pull"));
    }

    // A plan from a FUTURE schema major (> what this engine knows) must be
    // refused — the engine predates the wire change (DESIGN R1).
    #[test]
    fn future_schema_major_is_refused() {
        let mut plan = UpdatePlan::swap_only(1, vec![], None);
        plan.plan_schema = PLAN_SCHEMA_MAJOR + 1;
        assert!(!plan.schema_is_honorable());
    }

    #[test]
    fn empty_steps_is_swap_only() {
        let plan = UpdatePlan::swap_only(1, vec![], None);
        assert_eq!(plan.effective_steps(), vec!["swap", "relaunch"]);
        assert!(!plan.wants_step("pull"));
        assert!(!plan.wants_step("install"));
        assert!(!plan.wants_step("migrate"));
    }
}
