// SPDX-License-Identifier: AGPL-3.0-or-later
//! Cross-language parity test: Rust side.
//!
//! Consumes the same `tests/fixtures/project_naming.json` that
//! `tests/test_project_naming_parity.py` consumes. If this Rust
//! implementation of `canonical_class_prefix` diverges from the
//! recorded expectation, this test fails — exactly when the Python
//! parity test would also fail.
//!
//! Fixture path resolution: the fixture is at the orchestrator repo
//! root under `tests/fixtures/project_naming.json`. From the
//! `launcher/src-tauri/` crate root, that's `../../tests/fixtures/...`.
//! We resolve it relative to `CARGO_MANIFEST_DIR` so the test works
//! from any cwd (including `cargo test` invocations from the repo
//! root and from inside the crate dir).
//!
//! See `vco_lib/project_naming.py` for the rules-and-rationale write-up.

use serde::Deserialize;
use std::path::PathBuf;

use vct_launcher_temp_lib::project_naming::{canonical_class_prefix, CanonicalPrefixError};

/// Wire-format of the shared fixture. The `_comment` and `_format_version`
/// fields exist in the JSON but we don't need them in Rust beyond a
/// sanity assertion that the format version is what we expect.
#[derive(Debug, Deserialize)]
struct Fixture {
    #[serde(rename = "_format_version", default)]
    format_version: u32,
    cases: Vec<(String, String)>,
    errors: Vec<String>,
}

fn load_fixture() -> Fixture {
    // CARGO_MANIFEST_DIR is `<repo>/launcher/src-tauri/` at test time.
    // Walk up two levels to reach `<repo>/`, then descend into the
    // shared fixture path.
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir
        .parent()                          // <repo>/launcher
        .and_then(|p| p.parent())          // <repo>
        .expect("CARGO_MANIFEST_DIR doesn't have two parents — unexpected build layout");

    let fixture_path = repo_root.join("tests").join("fixtures").join("project_naming.json");
    assert!(
        fixture_path.exists(),
        "Parity fixture missing: {} — this file is shared with \
         tests/test_project_naming_parity.py",
        fixture_path.display()
    );

    let raw = std::fs::read_to_string(&fixture_path)
        .unwrap_or_else(|e| panic!("read fixture {}: {}", fixture_path.display(), e));
    let fix: Fixture = serde_json::from_str(&raw)
        .unwrap_or_else(|e| panic!("parse fixture {}: {}", fixture_path.display(), e));

    // Pin format version so a future schema extension is opt-in (one
    // side bumps the version, the other side notices and fails until
    // updated).
    assert_eq!(
        fix.format_version, 1,
        "Fixture _format_version != 1 — Python parity test may not \
         know how to parse this version; coordinate the bump across \
         both sides"
    );
    assert!(
        !fix.cases.is_empty(),
        "Fixture has no success cases — at minimum should pin MyProject and Camel_Case"
    );
    fix
}

#[test]
fn rust_canonical_class_prefix_matches_fixture_success_cases() {
    let fix = load_fixture();
    let mut failures: Vec<String> = Vec::new();

    for (input, expected) in &fix.cases {
        match canonical_class_prefix(input) {
            Ok(actual) => {
                if actual != *expected {
                    failures.push(format!(
                        "  canonical_class_prefix({:?}) = {:?}, fixture says {:?}",
                        input, actual, expected
                    ));
                }
            }
            Err(e) => {
                failures.push(format!(
                    "  canonical_class_prefix({:?}) errored ({}), fixture expected {:?}",
                    input, e, expected
                ));
            }
        }
    }

    assert!(
        failures.is_empty(),
        "Rust sanitizer diverges from fixture in {} case(s):\n{}\n\
         If this divergence is intentional, update both the fixture \
         AND the Python implementation in the same commit.",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn rust_canonical_class_prefix_rejects_fixture_error_cases() {
    let fix = load_fixture();
    let mut failures: Vec<String> = Vec::new();

    for input in &fix.errors {
        match canonical_class_prefix(input) {
            Ok(unexpected) => {
                failures.push(format!(
                    "  canonical_class_prefix({:?}) = Ok({:?}), but fixture says this input should error",
                    input, unexpected
                ));
            }
            Err(_) => { /* expected: any CanonicalPrefixError variant is fine */ }
        }
    }

    assert!(
        failures.is_empty(),
        "Rust sanitizer accepted {} input(s) that the fixture says should error:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

/// Sanity: the specific regression-pinning rows must be in the
/// fixture. If a future fixture-trim PR drops them, this test fails
/// loudly rather than silently un-pinning the bug 0.6 / 0.7 wedge.
#[test]
fn fixture_pins_known_collision_cases() {
    let fix = load_fixture();
    let map: std::collections::HashMap<&str, &str> = fix
        .cases
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();

    assert_eq!(
        map.get("Camel_Case"),
        Some(&"Camel_Case"),
        "Fixture must pin 'Camel_Case' → 'Camel_Case' (the base-host \
         escalation case for v0.2.15)"
    );
    assert_eq!(
        map.get("VibeCoded Orchestrator"),
        Some(&"VibeCodedOrchestrator"),
        "Fixture must pin 'VibeCoded Orchestrator' → 'VibeCodedOrchestrator' \
         (the original wedge case — wizard display must match)"
    );
    assert_eq!(
        map.get("vibecoded-orchestrator"),
        Some(&"Vibecoded_orchestrator"),
        "Fixture must pin 'vibecoded-orchestrator' → 'Vibecoded_orchestrator' \
         (the folder-name fallback case — proves --project is required \
         when invoking analyze_code_graph.py)"
    );
}

/// Error-variant smoke test independent of the fixture — ensures the
/// rejection branch returns the EXACT variants documented in the
/// public API, not just "some error".
#[test]
fn rust_error_variants_match_documented_api() {
    assert_eq!(
        canonical_class_prefix(""),
        Err(CanonicalPrefixError::Empty)
    );
    assert_eq!(
        canonical_class_prefix("   "),
        Err(CanonicalPrefixError::Empty)
    );
    assert!(matches!(
        canonical_class_prefix("123abc"),
        Err(CanonicalPrefixError::LeadingNonLetter { .. })
    ));
}
