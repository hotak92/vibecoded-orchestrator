// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.33 (Agent F, C2) integration tests for the manifest CI gate.
//!
//! These tests run as part of `cargo test -p vct-launcher-core` and
//! exercise:
//!   - `ModuleManifest::from_json` against the v0.2.7 RL fixture
//!     committed under `tests/fixtures/manifests/`.
//!   - The exported JSON schema (committed at
//!     `docs/schemas/vct-module.schema.json`) to assert
//!     `tauri_command` + `Unsupported` (via ConfigControlKnown's
//!     omission) appear in the right places.
//!   - Malformed input rejection: the binary must surface a clean
//!     error rather than panic.
//!
//! The tests run in strict-manifest mode so the lenient `Unsupported`
//! fallback doesn't mask schema errors. See
//! `vct_launcher_core::manifest::set_strict_manifest_for_test`.

use std::path::PathBuf;

use vct_launcher_core::manifest::ModuleManifest;

/// Locate `tests/fixtures/manifests/` relative to this crate's
/// `CARGO_MANIFEST_DIR`. Stable across `cargo test` invocations from
/// the workspace root or the crate dir.
fn fixtures_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("manifests")
}

fn repo_root() -> PathBuf {
    // launcher/src-tauri/vct-launcher-core → workspace root is three
    // `parent()` calls up. This is the same walk the existing
    // `vct_rl_reranker_manifest_deserializes` test uses.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .and_then(|p| p.parent())
        .expect("walk to repo root")
        .to_path_buf()
}

/// The committed v0.2.7 RL fixture MUST parse cleanly in strict mode.
/// Sentinel against the v0.2.32 regression class (silent manifest reject
/// → catalog fallback to builtin placeholder). If this test fails, a
/// paid-module fixture that real publishers ship has stopped
/// deserialising against the launcher's schema — typically because a
/// PR landed in `manifest.rs` that bumped a required field without a
/// default OR removed an enum variant.
#[test]
fn validate_manifest_binary_accepts_rl_v0_2_7_fixture() {
    vct_launcher_core::manifest::set_strict_manifest_for_test(true);
    let path = fixtures_dir().join("vct-rl-reranker.v0.2.7.json");
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
    let manifest = ModuleManifest::from_json(&raw)
        .unwrap_or_else(|e| panic!("v0.2.7 RL fixture must parse in strict mode: {}", e));
    assert_eq!(manifest.id, "vct-rl-reranker");
    assert_eq!(manifest.version, "0.2.7");
    assert!(
        manifest.gui.is_some(),
        "v0.2.7 manifest ships a gui.config_tab block",
    );
    // Reset to default lenient mode so subsequent tests aren't affected
    // (test harness runs in a single process; strict-mode is global).
    vct_launcher_core::manifest::set_strict_manifest_for_test(false);
}

/// Hand-rolled malformed manifest MUST be rejected with a clear error,
/// not a panic or a silent default. Catches the regression where adding
/// a new required field accidentally lands with `#[serde(default)]` that
/// quietly accepts nonsense input.
#[test]
fn validate_manifest_binary_rejects_garbage_input() {
    let garbage = r#"{"this": "is not a manifest"}"#;
    let result = ModuleManifest::from_json(garbage);
    assert!(
        result.is_err(),
        "garbage input must be rejected, got Ok({:?})",
        result.as_ref().ok().map(|m| &m.id),
    );
    let msg = result.unwrap_err();
    // The error should mention either the missing top-level field
    // (id, name, version) or be a clean JSON-shape error. We don't pin
    // the exact wording because serde error messages aren't stable
    // across versions — but it MUST be non-empty.
    assert!(!msg.is_empty(), "error message must be non-empty");
}

/// `tauri_command` step kind MUST appear in the exported schema.
/// Pinning this catches the regression where someone accidentally
/// removes `ActionDescriptor::TauriCommand` (Agent D, v0.2.33) without
/// realising it would break every paid module that uses it.
#[test]
fn schema_includes_tauri_command_variant() {
    let schema_path = repo_root().join("docs").join("schemas").join("vct-module.schema.json");
    let raw = std::fs::read_to_string(&schema_path)
        .unwrap_or_else(|e| panic!("read {}: {}", schema_path.display(), e));
    let schema: serde_json::Value = serde_json::from_str(&raw)
        .unwrap_or_else(|e| panic!("schema is not valid JSON: {}", e));
    let dump = schema.to_string();
    // We look for the literal `"tauri_command"` somewhere in the
    // schema's serialised form — schemars 0.8 emits the variant as
    // `"enum": ["tauri_command"]` inside an `ActionDescriptor` oneOf
    // branch. A simple substring check is fine because no other JSON
    // value would legitimately contain that string.
    assert!(
        dump.contains("\"tauri_command\""),
        "exported schema must contain 'tauri_command' — found a schema \
         that omits it, which means Agent D's variant is not derived. \
         Regenerate: cargo run -p vct-launcher-core --bin export-schema \
         --out docs/schemas/vct-module.schema.json",
    );
}

/// `Unsupported` ConfigControl variant MUST NOT appear in the exported
/// schema. It's a runtime-only forward-compat receptacle — exposing it
/// in the schema would invite publishers to declare it intentionally
/// (which makes no sense; the renderer skips it). The manual JsonSchema
/// impl on `ConfigControl` delegates to `ConfigControlKnown` precisely
/// to keep the published surface clean.
#[test]
fn schema_includes_unsupported_control_variant() {
    let schema_path = repo_root().join("docs").join("schemas").join("vct-module.schema.json");
    let raw = std::fs::read_to_string(&schema_path)
        .unwrap_or_else(|e| panic!("read {}: {}", schema_path.display(), e));
    // This test name asks "includes" but the answer is "must NOT" — the
    // inversion is deliberate (mirrors the spec wording). The schema
    // should advertise ONLY the strict-mode known variants so module
    // publishers don't accidentally declare runtime-only fallbacks.
    assert!(
        !raw.contains("\"Unsupported\""),
        "exported schema must NOT contain 'Unsupported' — it's a runtime-only \
         fallback for unknown control kinds, never something a publisher \
         should declare in their manifest. The custom JsonSchema impl on \
         ConfigControl delegates to ConfigControlKnown to ensure the public \
         schema only lists strict-mode variants. If this test fails, the \
         delegation broke or a derive(JsonSchema) was added to ConfigControl \
         directly.",
    );
    // Sanity: every KNOWN variant should be present, so the test fails
    // loudly if the schema goes empty for an unrelated reason.
    for kind in [
        "checkbox",
        "multi_select",
        "button",
        "select",
        "info",
        "text_input",
        "number_input",
        "status_display",
        "file_picker",
        "link",
        "info_dynamic",
        "date_picker",
    ] {
        let needle = format!("\"{}\"", kind);
        assert!(
            raw.contains(&needle),
            "schema missing known ConfigControl kind: {}",
            kind
        );
    }
}

/// CI invariant: the committed schema MUST match what
/// `cargo run --bin export-schema` would generate fresh. Drift catches
/// the case where someone edits `manifest.rs` but forgets to refresh
/// the schema artifact.
///
/// We don't shell out to the bin here — we replicate the same generator
/// path (`schemars::schema_for!(ModuleManifest)`) so the test is fast
/// and independent of the bin's CLI surface. The bin's `--check` flag
/// is the production-grade variant of this same comparison.
#[test]
fn exported_schema_matches_committed_copy() {
    let schema = schemars::schema_for!(ModuleManifest);
    let mut generated = serde_json::to_string_pretty(&schema)
        .expect("schemars output is always valid JSON");
    generated.push('\n');

    let schema_path = repo_root().join("docs").join("schemas").join("vct-module.schema.json");
    let committed = std::fs::read_to_string(&schema_path)
        .unwrap_or_else(|e| panic!("read {}: {}", schema_path.display(), e));

    if committed != generated {
        let committed_lines = committed.lines().count();
        let generated_lines = generated.lines().count();
        panic!(
            "committed schema is out of sync with manifest.rs.\n\
             committed: {} lines at {}\n\
             generated: {} lines (from schemars::schema_for!(ModuleManifest))\n\
             Refresh: cargo run -p vct-launcher-core --bin export-schema \
             --out docs/schemas/vct-module.schema.json",
            committed_lines,
            schema_path.display(),
            generated_lines,
        );
    }
}
