// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// The thin BOOTSTRAP STUB — the forever-stable bootstrap contract
// (DESIGN-v0300-update-system-architecture.md §3.1 "BOOTSTRAP STUB", §3.2).
//
// ============================================================================
// DORMANT in v0.2.60.
// ============================================================================
// This stub is COMPILED + unit-tested but is NOT wired as the update
// entrypoint. The LIVE update path in v0.2.60 is the launcher's
// `update_orchestrator` (installer.rs) — unchanged (Pieces 1-5). The stub is
// the v0.3.0 inverted entrypoint: it does the LEAST POSSIBLE work, then hands
// off to the (separate-process) engine which owns pull/install/migrate/swap.
//
// ============================================================================
// THE FOREVER-STABLE WIRE CONTRACT (the ONE thing kept backward-compatible
// ~forever — DESIGN §3.2, R1). Document it here because it is load-bearing:
// ============================================================================
//
//   1. The stub's job list (the 5 items below) is the stable surface. The
//      stub must remain runnable when EVERYTHING downstream is broken — it is
//      our `rustup-init` / Squirrel `Update.exe`. It MUST NOT depend on the
//      destination Python, the venv, Weaviate, or the hub. It is pure Rust +
//      filesystem + (in the networked case) one HTTPS download + a signature
//      check.
//
//   2. The `update.plan.json` wire schema (update_plan.rs) is the handoff.
//      `plan_schema` is a frozen MAJOR; within a major every field is
//      append-only / `#[serde(default)]`. An old stub refuses a plan whose
//      major it predates rather than mis-executing it.
//
//   The 5-item job list (the stub does ONLY these — DESIGN §3.1):
//     1. Arm the update gate (`.update-in-progress.json`). [launcher-side in
//        v0.3.0; the stub's analogue is `mark_gate_path`.]
//     2. Ensure/locate a TRUSTWORTHY engine binary:
//          - normal: download the single signed `vct-updater` artifact into
//            `<vct_root>/update-staging/`, verify sha256 (+ signature when
//            configured) BEFORE trusting it; OR
//          - offline/dev: use the `vct-updater` already on disk in the
//            destination clone (if present and >= running version).
//     3. Write `update.plan.json` (the v2 handoff).
//     4. Spawn the staged engine DETACHED with the plan path.
//     5. Exit (the launcher then releases launcher.db + binary locks).
//
// The stub here implements steps 2-4 as pure functions (locate+verify engine,
// build plan, decide spawn). The actual download (HTTPS) + the launcher
// `app.exit(0)` (step 5) are the launcher's job in v0.3.0; the stub provides
// the verify+plan+spawn-decision core so it can be unit-tested dormant.

// DORMANT in v0.2.60: the stub is not wired as the update entrypoint
// (update_orchestrator stays live). dead_code is intentional until v0.3.0.
#![allow(dead_code)]

use std::path::{Path, PathBuf};

use crate::signature::{self, TrustOutcome};
use crate::update_plan::{PlanSwap, UpdatePlan, PLAN_SCHEMA_MAJOR};

/// Why the stub chose a particular engine source (forensic / test surface).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EngineSource {
    /// Used the freshly-downloaded + verified staged artifact.
    StagedDownload(PathBuf),
    /// Offline/dev fallback: used the engine already on disk in the clone.
    OnDiskFallback(PathBuf),
}

/// The decision the stub reaches after locating + verifying the engine.
/// Either "spawn this engine with this plan" or "refuse — here's why".
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BootstrapDecision {
    /// Verified engine + a plan ready to hand off.
    Spawn {
        engine: PathBuf,
        source: EngineSource,
        trust: TrustOutcome,
        plan: Box<UpdatePlan>,
    },
    /// The stub refuses to proceed (untrusted engine, no engine found, etc.).
    /// The caller surfaces this and does NOT exit the launcher.
    Refuse(String),
}

/// Inputs the stub needs to build a handoff plan (gathered launcher-side in
/// v0.3.0; a struct so the dormant logic is unit-testable).
#[derive(Debug, Clone)]
pub struct BootstrapInputs {
    pub install_root: PathBuf,
    pub vct_root: PathBuf,
    pub from_version: String,
    pub to_version: String,
    pub upstream_remote: String,
    pub branch: String,
    pub parent_pid: u32,
    /// The binaries the engine will swap (launcher + hub). On POSIX these are
    /// recorded for the plan but the swap is a no-op (engine dispatch).
    pub swap_targets: Vec<PathBuf>,
    /// Where the launcher binary lives (the engine relaunches it).
    pub relaunch: PathBuf,
    /// The pinned sha256 the stub must verify the engine artifact against
    /// (from the signed release manifest / `.sha256` sidecar). None → the
    /// stub can only use the offline on-disk fallback (no integrity anchor
    /// for a download).
    pub expected_engine_sha256: Option<String>,
    /// ISO-8601 timestamp (the launcher supplies wall-clock).
    pub started_at: String,
}

/// Locate + verify the engine binary, then build the handoff plan. PURE: no
/// process spawn, no download, no clock — so it is fully unit-testable. The
/// caller (the v0.3.0 launcher stub) does the HTTPS download into
/// `staged_download` before calling, then spawns the returned engine.
///
/// Trust gate: a `staged_download` is used ONLY if it verifies against
/// `expected_engine_sha256` (`TrustOutcome::may_exec()`). If it doesn't
/// verify (or there's no expected sha), the stub falls back to the on-disk
/// engine in the clone — which is trusted by provenance (it shipped in the
/// signed release archive the user already installed), so it needs no
/// re-download verification.
pub fn decide(
    inputs: &BootstrapInputs,
    staged_download: Option<&Path>,
    on_disk_engine: Option<&Path>,
) -> BootstrapDecision {
    // ── Step 2: ensure/locate a TRUSTWORTHY engine ──────────────────────
    let (engine, source, trust): (PathBuf, EngineSource, TrustOutcome) = match staged_download {
        Some(staged) if staged.is_file() => {
            // A download must be verified before we trust it.
            let Some(expected) = inputs.expected_engine_sha256.as_deref() else {
                // No integrity anchor for the download → cannot trust it.
                // Fall through to the on-disk fallback below.
                return fallback_or_refuse(inputs, on_disk_engine,
                    "downloaded engine present but no expected sha256 to verify it against");
            };
            match signature::verify_artifact(staged, expected, None, None) {
                Ok(outcome) if outcome.may_exec() => (
                    staged.to_path_buf(),
                    EngineSource::StagedDownload(staged.to_path_buf()),
                    outcome,
                ),
                Ok(outcome) => {
                    return BootstrapDecision::Refuse(format!(
                        "refusing downloaded engine — trust check failed: {}",
                        outcome.detail
                    ));
                }
                Err(e) => {
                    return BootstrapDecision::Refuse(format!(
                        "refusing downloaded engine — verify error: {}",
                        e
                    ));
                }
            }
        }
        // No (or non-existent) staged download → offline/dev fallback.
        _ => match on_disk_engine {
            Some(p) if p.is_file() => (
                p.to_path_buf(),
                EngineSource::OnDiskFallback(p.to_path_buf()),
                // The on-disk engine shipped in the signed release archive the
                // user installed — trusted by provenance; no re-verify needed.
                TrustOutcome {
                    sha256_ok: true,
                    signature: signature::SignatureCheck::NotConfigured,
                    detail: "on-disk engine trusted by install provenance \
                             (shipped in the verified release archive)"
                        .into(),
                },
            ),
            _ => {
                return BootstrapDecision::Refuse(
                    "no trustworthy engine binary found (no verified download, \
                     no on-disk fallback)"
                        .into(),
                );
            }
        },
    };

    // ── Step 3: build the update.plan.json handoff ──────────────────────
    let plan = build_plan(inputs, &engine, &trust);
    BootstrapDecision::Spawn {
        engine,
        source,
        trust,
        plan: Box::new(plan),
    }
}

/// Try the on-disk fallback; refuse with a combined reason if it's absent.
fn fallback_or_refuse(
    inputs: &BootstrapInputs,
    on_disk_engine: Option<&Path>,
    why_download_failed: &str,
) -> BootstrapDecision {
    match on_disk_engine {
        Some(p) if p.is_file() => {
            let trust = TrustOutcome {
                sha256_ok: true,
                signature: signature::SignatureCheck::NotConfigured,
                detail: format!(
                    "{}; using on-disk engine (trusted by install provenance)",
                    why_download_failed
                ),
            };
            let plan = build_plan(inputs, p, &trust);
            BootstrapDecision::Spawn {
                engine: p.to_path_buf(),
                source: EngineSource::OnDiskFallback(p.to_path_buf()),
                trust,
                plan: Box::new(plan),
            }
        }
        _ => BootstrapDecision::Refuse(format!(
            "{}; and no on-disk fallback engine available — refusing",
            why_download_failed
        )),
    }
}

/// Build the `update.plan.json` from the gathered inputs + chosen engine.
fn build_plan(inputs: &BootstrapInputs, engine: &Path, trust: &TrustOutcome) -> UpdatePlan {
    UpdatePlan {
        parent_pid: inputs.parent_pid,
        swaps: inputs
            .swap_targets
            .iter()
            .map(|t| PlanSwap { target: t.clone() })
            .collect(),
        relaunch: Some(inputs.relaunch.clone()),
        started_at: Some(inputs.started_at.clone()),
        plan_schema: PLAN_SCHEMA_MAJOR,
        install_root: Some(inputs.install_root.clone()),
        vct_root: Some(inputs.vct_root.clone()),
        from_version: Some(inputs.from_version.clone()),
        to_version: Some(inputs.to_version.clone()),
        upstream_remote: Some(inputs.upstream_remote.clone()),
        branch: Some(inputs.branch.clone()),
        engine_binary: Some(engine.to_path_buf()),
        engine_sha256: inputs.expected_engine_sha256.clone(),
        // The inverted engine owns the full sequence.
        steps: vec![
            "pull".into(),
            "install".into(),
            "migrate".into(),
            "swap".into(),
            "relaunch".into(),
        ],
        hard_cut: false,
    }
    .also_log(&trust.detail)
}

trait AlsoLog {
    fn also_log(self, _detail: &str) -> Self;
}
impl AlsoLog for UpdatePlan {
    fn also_log(self, _detail: &str) -> Self {
        // Hook for forensic logging when the stub is wired (v0.3.0). Dormant
        // no-op now; kept so the call-site shape is stable.
        self
    }
}

/// The conventional staging directory the stub downloads the engine into.
/// `<vct_root>/update-staging/`.
pub fn staging_dir(vct_root: &Path) -> PathBuf {
    vct_root.join("update-staging")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn inputs_with(expected_sha: Option<String>) -> (tempfile::TempDir, BootstrapInputs) {
        let td = tempfile::tempdir().unwrap();
        let root = td.path();
        let inputs = BootstrapInputs {
            install_root: root.join("clone"),
            vct_root: root.join(".vct"),
            from_version: "0.3.0".into(),
            to_version: "0.3.1".into(),
            upstream_remote: "vco_upstream".into(),
            branch: "main".into(),
            parent_pid: 4242,
            swap_targets: vec![root.join("vct-launcher")],
            relaunch: root.join("vct-launcher"),
            expected_engine_sha256: expected_sha,
            started_at: "2026-06-16T00:00:00Z".into(),
        };
        (td, inputs)
    }

    fn write_file(dir: &Path, name: &str, bytes: &[u8]) -> PathBuf {
        let p = dir.join(name);
        let mut f = std::fs::File::create(&p).unwrap();
        f.write_all(bytes).unwrap();
        p
    }

    // sha256("abc")
    const ABC_SHA: &str = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

    #[test]
    fn verified_download_is_spawned_with_full_plan() {
        let (td, inputs) = inputs_with(Some(ABC_SHA.into()));
        let staged = write_file(td.path(), "vct-updater-staged", b"abc");
        let decision = decide(&inputs, Some(&staged), None);
        match decision {
            BootstrapDecision::Spawn { source, plan, trust, .. } => {
                assert!(matches!(source, EngineSource::StagedDownload(_)));
                assert!(trust.may_exec());
                // The plan declares the full inverted step set.
                assert_eq!(
                    plan.steps,
                    vec!["pull", "install", "migrate", "swap", "relaunch"]
                );
                assert_eq!(plan.plan_schema, PLAN_SCHEMA_MAJOR);
                assert_eq!(plan.parent_pid, 4242);
            }
            other => panic!("expected Spawn, got {:?}", other),
        }
    }

    // A download whose sha256 does NOT match the pinned digest is REFUSED —
    // never spawned (the signature/trust gate, DESIGN R3).
    #[test]
    fn tampered_download_is_refused() {
        let (td, inputs) = inputs_with(Some(ABC_SHA.into()));
        // Bytes that hash to something else.
        let staged = write_file(td.path(), "vct-updater-staged", b"TAMPERED");
        let decision = decide(&inputs, Some(&staged), None);
        match decision {
            BootstrapDecision::Refuse(why) => {
                assert!(
                    why.contains("trust check failed") || why.contains("MISMATCH"),
                    "refusal must name the trust failure, got: {}",
                    why
                );
            }
            other => panic!("expected Refuse, got {:?}", other),
        }
    }

    // A download with NO expected sha (no integrity anchor) falls back to the
    // on-disk engine when present.
    #[test]
    fn download_without_sha_falls_back_to_on_disk() {
        let (td, inputs) = inputs_with(None);
        let staged = write_file(td.path(), "vct-updater-staged", b"abc");
        let on_disk = write_file(td.path(), "vct-updater-ondisk", b"whatever");
        let decision = decide(&inputs, Some(&staged), Some(&on_disk));
        match decision {
            BootstrapDecision::Spawn { source, .. } => {
                assert!(matches!(source, EngineSource::OnDiskFallback(_)));
            }
            other => panic!("expected on-disk fallback Spawn, got {:?}", other),
        }
    }

    // Offline (no download) uses the on-disk engine (dev/airgapped — DESIGN R5).
    #[test]
    fn offline_uses_on_disk_engine() {
        let (td, inputs) = inputs_with(None);
        let on_disk = write_file(td.path(), "vct-updater", b"engine");
        let decision = decide(&inputs, None, Some(&on_disk));
        assert!(matches!(decision, BootstrapDecision::Spawn { .. }));
    }

    // No engine anywhere → refuse (never spawn an absent/untrusted thing).
    #[test]
    fn no_engine_anywhere_refuses() {
        let (_td, inputs) = inputs_with(Some(ABC_SHA.into()));
        let decision = decide(&inputs, None, None);
        assert!(matches!(decision, BootstrapDecision::Refuse(_)));
    }

    #[test]
    fn staging_dir_under_vct_root() {
        assert_eq!(
            staging_dir(Path::new("/home/u/.vct")),
            PathBuf::from("/home/u/.vct/update-staging")
        );
    }
}
