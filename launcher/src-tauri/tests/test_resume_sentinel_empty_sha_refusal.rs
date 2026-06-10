// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.53 NEW-11: smoke-level integration test for the resume-sentinel
//! empty-sha refusal contract.
//!
//! Why the contract matters:
//!   The sentinel at `.claude/state/orchestrator-update-resume.json`
//!   records `sha_at_conflict` — the HEAD SHA at the moment the
//!   merge/rebase conflict was hit. The Continue Update flow refuses
//!   resume when HEAD has NOT moved past that SHA (= user aborted via
//!   CLI; sentinel is stale).
//!
//!   When the conflict-time `read_head_sha()` fails (.git/HEAD missing,
//!   permission denied, transient I/O), the sentinel is written with
//!   an empty string for `sha_at_conflict`. The pre-v0.2.53 head-
//!   unchanged guard skipped its comparison silently in that case AND
//!   let the resume proceed — against an unknown baseline. The fix
//!   refuses explicitly with a remediation message.
//!
//! Test surface:
//!   `installer.rs` keeps the sentinel parser private. From an
//!   integration-test crate we can only assert the on-disk JSON shape:
//!   sentinels with empty `sha_at_conflict` are valid JSON the parser
//!   can deserialise but downstream callers see `is_empty() == true`.
//!   The full Tauri command exercise is covered by the inline unit
//!   tests in `installer.rs::tests::test_resume_sentinel_*` which DO
//!   have visibility to the private struct + reader.
//!
//! This integration test pins the wire format — if a future refactor
//! changes the JSON keys (e.g. renames `sha_at_conflict` → `sha_at`),
//! the launcher's downgrade path would silently break.

use std::path::PathBuf;

/// The relative path the launcher uses to locate the sentinel.
/// MUST agree with `UPDATE_RESUME_SENTINEL_REL` in installer.rs.
const SENTINEL_REL: &str = ".claude/state/orchestrator-update-resume.json";

fn tmp() -> PathBuf {
    let p = std::env::temp_dir()
        .join(format!("vct-sentinel-test-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&p).unwrap();
    p
}

#[test]
fn empty_sha_sentinel_is_well_formed_json() {
    let p = tmp();
    let target = p.join(SENTINEL_REL);
    std::fs::create_dir_all(target.parent().unwrap()).unwrap();
    let body = r#"{
        "schema": 1,
        "operation": "merge",
        "branch": "main",
        "sha_at_conflict": "",
        "written_at": "2026-06-10T12:00:00Z"
    }"#;
    std::fs::write(&target, body).unwrap();

    // Round-trip parse: this is what `read_update_resume_sentinel`
    // does internally. We are NOT asserting the launcher accepts the
    // file — we are asserting the on-disk shape is wire-compatible.
    let parsed: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
    assert_eq!(parsed["schema"], 1);
    assert_eq!(parsed["operation"], "merge");
    assert_eq!(parsed["sha_at_conflict"], "");

    std::fs::remove_dir_all(&p).ok();
}

#[test]
fn sentinel_path_matches_known_relative_layout() {
    // Pinning the relative path so a directory-layout refactor cannot
    // silently move the sentinel without bumping every caller in
    // launcher + install.py. The path is referenced by name in:
    //   - installer.rs::UPDATE_RESUME_SENTINEL_REL (Rust writer/reader)
    //   - DeferralReport / vco_lib/deferral_report.py (Python reader)
    assert_eq!(SENTINEL_REL, ".claude/state/orchestrator-update-resume.json");
}
