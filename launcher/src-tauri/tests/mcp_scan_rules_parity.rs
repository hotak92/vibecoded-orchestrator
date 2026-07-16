// SPDX-License-Identifier: AGPL-3.0-or-later
//! Cross-language parity test: Rust side (v0.2.83 WP-B4).
//!
//! Reads the SAME `vco_lib/mcp_scan_rules.toml` that
//! `tests/test_mcp_scan_rules_parity.py` consumes, from DISK at test time
//! (not the compile-time-embedded copy), and asserts the Rust-visible rules
//! equal the committed file. If the embedded copy in the binary ever drifts
//! from the on-disk table, this fails — exactly when the Python parity test
//! would also fail.
//!
//! Fixture path resolution mirrors `project_naming_parity.rs`: the table is
//! at the orchestrator repo root under `vco_lib/mcp_scan_rules.toml`. From
//! the `launcher/src-tauri/` crate root that's `../../vco_lib/...`. We
//! resolve it relative to `CARGO_MANIFEST_DIR` so the test works from any
//! cwd.
//!
//! See `vco_lib/mcp_scan_rules.toml` for the rules-and-rationale write-up.

use std::path::PathBuf;

use vct_launcher_core::mcp_scan_rules;

fn table_path() -> PathBuf {
    // CARGO_MANIFEST_DIR is `<repo>/launcher/src-tauri/` at test time.
    // Walk up two levels to reach `<repo>/`, then into `vco_lib/`.
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir
        .parent() // <repo>/launcher
        .and_then(|p| p.parent()) // <repo>
        .expect("CARGO_MANIFEST_DIR doesn't have two parents — unexpected build layout");
    repo_root.join("vco_lib").join("mcp_scan_rules.toml")
}

fn load_disk_table() -> mcp_scan_rules::McpScanRules {
    let path = table_path();
    assert!(
        path.exists(),
        "Parity table missing: {} — this file is shared with \
         tests/test_mcp_scan_rules_parity.py",
        path.display()
    );
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("read table {}: {}", path.display(), e));
    mcp_scan_rules::parse_str(&raw)
        .unwrap_or_else(|e| panic!("parse table {}: {}", path.display(), e))
}

/// The compile-time-embedded rules (what the shipped binary actually uses)
/// must equal the on-disk committed table. Guards against the embedded copy
/// silently diverging from the file the Python side reads.
#[test]
fn embedded_rules_match_on_disk_table() {
    let disk = load_disk_table();
    let embedded = &*mcp_scan_rules::RULES;
    assert_eq!(
        embedded.allowed_global_env_keys, disk.allowed_global_env_keys,
        "embedded allowed_global_env_keys drifted from the on-disk table"
    );
    assert_eq!(
        embedded.secret_shaped_needles, disk.secret_shaped_needles,
        "embedded secret_shaped_needles drifted from the on-disk table"
    );
    assert_eq!(
        embedded.default_mcp_entry_names, disk.default_mcp_entry_names,
        "embedded default_mcp_entry_names drifted from the on-disk table"
    );
}

/// The public accessors return the on-disk table's values. This is the
/// surface `mcp_registration.rs` consumes, so it must equal the committed
/// file byte-for-byte (order included).
#[test]
fn accessors_match_on_disk_table() {
    let disk = load_disk_table();
    assert_eq!(
        mcp_scan_rules::allowed_global_env_keys(),
        disk.allowed_global_env_keys.as_slice(),
    );
    assert_eq!(
        mcp_scan_rules::secret_shaped_needles(),
        disk.secret_shaped_needles.as_slice(),
    );
    assert_eq!(
        mcp_scan_rules::default_mcp_entry_names(),
        disk.default_mcp_entry_names.as_slice(),
    );
}

/// Sanity: the canonical values the whole system depends on are present in
/// the committed table (regression pin — a fixture trim can't silently drop
/// them).
#[test]
fn table_pins_known_values() {
    let disk = load_disk_table();
    assert_eq!(
        disk.default_mcp_entry_names,
        vec!["weaviate-kg", "search", "playwright", "mermaid", "excalidraw"],
        "the builder-composed default entry names must be pinned"
    );
    assert!(
        disk.allowed_global_env_keys.contains(&"WEAVIATE_URL".to_string()),
        "WEAVIATE_URL must be in the env allowlist"
    );
    assert!(
        !disk.allowed_global_env_keys.contains(&"KG_COLLECTION".to_string()),
        "KG_COLLECTION must NOT be in the global env allowlist (per-project)"
    );
    assert!(
        disk.secret_shaped_needles.contains(&"TOKEN".to_string()),
        "TOKEN must be a secret-shaped needle"
    );
}
