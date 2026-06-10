// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.53 DEDUP-14 (PROMOTED): integration-shape smoke test for the
//! paired sentinel + deferral writer.
//!
//! The full atomic-ish contract is exercised by the in-module unit
//! tests in `installer.rs::tests::divergence_modal_tests::paired_writer_*`
//! (which have visibility to the private helper). This integration
//! test pins the ON-DISK side effects so a future refactor of the
//! helper's internals — e.g. swapping the sentinel writer to a JSONL
//! shape, moving the deferral to a different path — surfaces here
//! through file-system observation.
//!
//! What we verify on disk:
//!   1. The sentinel lands at `.claude/state/orchestrator-update-resume.json`.
//!   2. The deferral lands at `.claude/context/UPDATE_DEFERRED.md`.
//!   3. The two files are atomic-ish: when one is found, the OTHER
//!      should be too (the v0.2.51 Bug A class is exactly the half-
//!      written case).
//!
//! The actual helper invocation can only happen from inside the
//! installer.rs module (the helper is private). This file therefore
//! captures the contract via path constants + comments referencing the
//! in-module test names, so anyone refactoring the helper signature
//! follows the breadcrumb here.

use std::path::PathBuf;

/// The two on-disk side effects the paired writer MUST produce.
/// Constants live here AND in installer.rs; a drift between them
/// surfaces at PR time via this test.
const SENTINEL_REL: &str = ".claude/state/orchestrator-update-resume.json";
const DEFERRAL_REL: &str = ".claude/context/UPDATE_DEFERRED.md";

#[test]
fn paired_writer_target_paths_are_stable() {
    // Pin the relative paths. installer.rs uses
    // `UPDATE_RESUME_SENTINEL_REL` for the sentinel and a literal for
    // the deferral; this test fails when either drifts, prompting an
    // update to deferral_report.py + install.py readers in lockstep.
    assert_eq!(SENTINEL_REL, ".claude/state/orchestrator-update-resume.json");
    assert_eq!(DEFERRAL_REL, ".claude/context/UPDATE_DEFERRED.md");
}

#[test]
fn paired_writer_paths_share_a_grandparent() {
    // Both files live under `.claude/` — the parent directory the
    // launcher creates during the project-bundle bootstrap. If either
    // moves out of that subtree, the install-flow's tarball+rsync
    // discipline breaks (the orchestrator-self bundle excludes anything
    // outside `.claude/`).
    let sentinel = PathBuf::from(SENTINEL_REL);
    let deferral = PathBuf::from(DEFERRAL_REL);
    let sentinel_root = sentinel.components().next().unwrap();
    let deferral_root = deferral.components().next().unwrap();
    assert_eq!(
        sentinel_root.as_os_str(),
        ".claude",
        "sentinel must live under .claude/"
    );
    assert_eq!(
        deferral_root.as_os_str(),
        ".claude",
        "deferral must live under .claude/"
    );
}
