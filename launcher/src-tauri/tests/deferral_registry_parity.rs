// SPDX-License-Identifier: AGPL-3.0-or-later
//! Cross-language parity test: Rust side (v0.2.91 WP-B).
//!
//! Reads the SAME `vco_lib/deferral_conditions.toml` that
//! `tests/test_deferral_registry_parity_v0291.py` consumes, from DISK at test
//! time (not the compile-time-embedded copy), and asserts the Rust-visible
//! registry equals the committed file. If the embedded copy in the binary ever
//! drifts from the on-disk table, this fails — exactly when the Python parity
//! test would also fail.
//!
//! It additionally pins the two things a mirror can silently get wrong even
//! while parsing the same bytes: the LOOKUP ORDER (longest glob first) and the
//! GLOB SEMANTICS. A registry that parses identically but resolves differently
//! is worse than no mirror at all — the launcher would badge a condition the
//! CLI treats as a record, or vice versa.
//!
//! Fixture path resolution mirrors `mcp_scan_rules_parity.rs`.

use std::path::PathBuf;

use vct_launcher_core::deferral_registry;

fn table_path() -> PathBuf {
    // CARGO_MANIFEST_DIR is `<repo>/launcher/src-tauri/` at test time.
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir
        .parent() // <repo>/launcher
        .and_then(|p| p.parent()) // <repo>
        .expect("CARGO_MANIFEST_DIR doesn't have two parents — unexpected build layout");
    repo_root.join("vco_lib").join("deferral_conditions.toml")
}

fn load_disk_table() -> deferral_registry::DeferralRegistry {
    let path = table_path();
    assert!(
        path.exists(),
        "Parity table missing: {} — this file is shared with \
         tests/test_deferral_registry_parity_v0291.py",
        path.display()
    );
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("read table {}: {}", path.display(), e));
    deferral_registry::parse_str(&raw)
        .unwrap_or_else(|e| panic!("parse table {}: {}", path.display(), e))
}

/// The compile-time-embedded registry (what the shipped binary uses) must
/// equal the on-disk committed table.
#[test]
fn embedded_registry_matches_on_disk_table() {
    let disk = load_disk_table();
    let embedded = &*deferral_registry::REGISTRY;
    assert_eq!(
        embedded, &disk,
        "the embedded deferral_conditions.toml drifted from the on-disk table"
    );
}

/// Every registered pattern resolves to the same disposition through both the
/// disk-loaded and the embedded registry — a per-row check, so a failure names
/// the row instead of dumping the whole struct.
#[test]
fn every_pattern_resolves_identically() {
    let disk = load_disk_table();
    let embedded = &*deferral_registry::REGISTRY;
    for pattern in disk.patterns() {
        // Substitute the wildcard so a glob pattern becomes a concrete id.
        let concrete = pattern.replace('*', "x");
        assert_eq!(
            disk.disposition_for(&concrete),
            embedded.disposition_for(&concrete),
            "disposition drift for {}",
            pattern
        );
    }
}

/// The ownership sets Rust derives must be exactly what Python derives —
/// `tests/test_deferral_registry_parity_v0291.py` asserts the same values from
/// the Python side, so the two together pin both loaders to one answer.
#[test]
fn ownership_sets_are_stable() {
    let r = &*deferral_registry::REGISTRY;
    let prefixes = r.install_owned_prefixes();
    assert_eq!(
        prefixes,
        vec![
            "bundle_pin_drift_".to_string(),
            "deprecated_mcp_".to_string(),
            "kg_named_vector_slot_error_".to_string(),
            "lowercase_codegraph_residual_".to_string(),
            "schema_migration_failed_".to_string(),
            "schema_migration_required_".to_string(),
            "stale_unit_retired_".to_string(),
        ],
        "owned prefix families drifted — update install.py's pin test in the \
         same commit"
    );
    let owned = r.install_owned_ids();
    // Spot-pin the four v0.2.91 additions: record-class cids promoted to
    // one-shot auto-expiry via registry ownership.
    for cid in [
        "kg_access_phantom_repaired",
        "codegraph_binding_repaired",
        "launcher_binary_clobber_averted",
        "hard_cut_performed",
    ] {
        assert!(
            owned.iter().any(|o| o == cid),
            "{cid} must be install-owned so its record auto-expires"
        );
    }
    // And a cid that must NEVER be owned: a foreign, probe-cleared condition
    // wrongly listed as owned is the A-2 data-loss bug.
    assert!(
        !owned.iter().any(|o| o == "codegraph_embed_resync_pending"),
        "codegraph_embed_resync_pending is owed-work, resolved by a positive \
         probe — owning it would silently clobber it every update"
    );
}

/// The table only uses `*` wildcards. The Rust matcher deliberately does not
/// implement `?` / `[...]`, so a row using them would resolve differently in
/// the two languages; catch it here rather than in the field.
#[test]
fn patterns_use_only_star_wildcards() {
    let disk = load_disk_table();
    for pattern in disk.patterns() {
        assert!(
            !pattern.contains('?') && !pattern.contains('[') && !pattern.contains(']'),
            "pattern {pattern:?} uses an fnmatch feature the Rust mirror does \
             not implement; restrict the table to '*' wildcards"
        );
    }
}
