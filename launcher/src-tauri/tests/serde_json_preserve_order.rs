// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.92 (WP-19): `serde_json`'s `preserve_order` feature is load-bearing,
//! and this file is its live consumer.
//!
//! Every JSON file the launcher rewrites — `~/.claude/settings.json` (the
//! Artifact panel), `~/.claude.json` (MCP registration), per-project
//! `.claude/settings.json` and `.vscode/settings.json` — is a
//! read-modify-write of a document the USER owns. Without `preserve_order`,
//! `Value::Object` is backed by a `BTreeMap` and every such write silently
//! alphabetises the user's keys.
//!
//! Two things can quietly undo that, and each has a test here:
//!
//! 1. **The feature getting dropped from a manifest.** It is declared ONCE,
//!    in `launcher/src-tauri/Cargo.toml` `[workspace.dependencies]`, and
//!    inherited by the four workspace members — but `launcher/tools/vct-cli`
//!    is a SEPARATE workspace (its own `Cargo.lock`) and cannot inherit, so
//!    it carries its own copy. `manifests_declare_preserve_order` reads all
//!    five manifests and pins both halves of that arrangement.
//!
//! 2. **The feature being declared but not actually reaching the build.**
//!    A manifest assertion alone would be a promise about a file, not about
//!    the compiled binary. `behaviour_*` assert the runtime property, so a
//!    resolver change or a stray `default-features = false` fails here.
//!
//! Note that a third guard needs no test: `Map::shift_remove` only exists
//! WITH `preserve_order`, so the call-sites that use it (see
//! `src/json_file.rs`'s module docs) will not compile without the feature.
//! Losing it is a build error, not a silent behaviour regression.

use std::path::{Path, PathBuf};

/// Repo root. `CARGO_MANIFEST_DIR` is `launcher/src-tauri`, so two
/// `parent()` calls. Same walk the sibling parity tests use.
fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .expect("walk to repo root")
        .to_path_buf()
}

/// Join a repo-relative path one component at a time.
///
/// `Path::join("a/b/c")` happens to work on Windows too, but every other
/// path-building test in this tree spells the components out, and matching
/// that convention keeps the separator question from being a per-file
/// judgement call.
fn at(parts: &[&str]) -> PathBuf {
    let mut p = repo_root();
    for part in parts {
        p.push(part);
    }
    p
}

/// Manifest body with full-line TOML comments stripped.
///
/// These manifests explain the `preserve_order` arrangement in prose that
/// necessarily quotes the very forms this test forbids, so a naive
/// `contains` over the raw bytes reads a comment as a declaration. (It did:
/// the first run of this test failed on its own explanatory comment.) The
/// assertions below are about what cargo READS, so this is what they get.
fn read(path: &Path) -> String {
    let raw = std::fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
    raw.lines()
        .filter(|line| !line.trim_start().starts_with('#'))
        .collect::<Vec<_>>()
        .join("\n")
}

/// All five manifests that declare `serde_json` must end up with
/// `preserve_order`: the workspace root declares it, the three members
/// inherit it, and the out-of-workspace CLI repeats it verbatim.
///
/// A mixed set is the failure this catches. Cargo unifies features across
/// the packages in a build graph, so a member that reverted to a bare
/// `serde_json = "1"` would still get the feature under
/// `cargo build --workspace` and NOT under `cargo build -p <member>` — the
/// same source compiled two ways, writing two different byte orders.
#[test]
fn manifests_declare_preserve_order() {
    // The one home.
    let ws = read(&at(&["launcher", "src-tauri", "Cargo.toml"]));
    assert!(
        ws.contains("[workspace.dependencies]"),
        "launcher/src-tauri/Cargo.toml must keep a [workspace.dependencies] table"
    );
    assert!(
        ws.contains(r#"serde_json = { version = "1", features = ["preserve_order"] }"#),
        "the workspace serde_json entry must carry the preserve_order feature"
    );

    // The inheritors. `serde_json.workspace = true` is the ONLY accepted
    // form — a re-declared `serde_json = "1"` would silently drop the
    // feature for single-crate builds.
    for member in [
        &["launcher", "src-tauri", "Cargo.toml"][..],
        &["launcher", "src-tauri", "vct-launcher-core", "Cargo.toml"][..],
        &["launcher", "src-tauri", "vct-hub", "Cargo.toml"][..],
        &["launcher", "src-tauri", "vct-updater", "Cargo.toml"][..],
    ] {
        let path = at(member);
        let body = read(&path);
        let shown = path.display();
        assert!(
            body.contains("serde_json.workspace = true"),
            "{shown} must inherit serde_json from [workspace.dependencies]"
        );
        assert!(
            !body.contains(r#"serde_json = "1""#),
            "{shown} re-declares serde_json instead of inheriting it — the \
             feature would then differ between a workspace build and a \
             `cargo build -p` build"
        );
    }

    // The one that cannot inherit. Separate workspace, separate Cargo.lock:
    // nothing propagates from src-tauri to here.
    let cli = read(&at(&["launcher", "tools", "vct-cli", "Cargo.toml"]));
    assert!(
        cli.contains(r#"serde_json = { version = "1", features = ["preserve_order"] }"#),
        "launcher/tools/vct-cli is a separate workspace and MUST repeat the \
         preserve_order feature; it inherits nothing from src-tauri"
    );
}

/// The compiled behaviour, not the manifest text: parsing and re-emitting a
/// document keeps the author's key order.
#[test]
fn behaviour_a_parse_and_reemit_round_trip_keeps_the_authors_order() {
    // Neither alphabetical nor reverse-alphabetical: a sort in either
    // direction changes this.
    let src = r#"{"zebra":1,"apple":2,"mango":3,"banana":4}"#;
    let v: serde_json::Value = serde_json::from_str(src).unwrap();
    assert_eq!(
        serde_json::to_string(&v).unwrap(),
        src,
        "a parse→emit round trip must be byte-identical, which it is only \
         with serde_json's preserve_order feature enabled"
    );
}

/// The `json!` macro likewise emits literal order, not sorted order.
#[test]
fn behaviour_the_json_macro_emits_literal_order() {
    let v = serde_json::json!({"zebra": 1, "apple": 2});
    assert_eq!(serde_json::to_string(&v).unwrap(), r#"{"zebra":1,"apple":2}"#);
}

/// `Value` equality stays order-INSENSITIVE under `preserve_order`
/// (`IndexMap`'s `PartialEq` compares as a map, not as a sequence).
///
/// Pinned because the whole suite's `assert_eq!(value, json!({..}))` idiom
/// rests on it: if this ever became order-sensitive, hundreds of unrelated
/// tests would start failing for reasons that have nothing to do with what
/// they test.
#[test]
fn behaviour_value_equality_ignores_key_order() {
    let a: serde_json::Value = serde_json::from_str(r#"{"x":1,"y":2}"#).unwrap();
    let b: serde_json::Value = serde_json::from_str(r#"{"y":2,"x":1}"#).unwrap();
    assert_eq!(a, b, "Value equality must remain order-insensitive");
    assert_ne!(
        serde_json::to_string(&a).unwrap(),
        serde_json::to_string(&b).unwrap(),
        "…while their SERIALISATIONS differ, which is the point of the feature"
    );
}

/// `shift_remove` closes the gap; the bare `remove` that `preserve_order`
/// redefines as `swap_remove` pulls the tail forward.
///
/// This is the hazard the module docs in `src/json_file.rs` put on every
/// caller, pinned once at the crate boundary as well as at each call-site.
#[test]
fn behaviour_shift_remove_preserves_order_where_remove_does_not() {
    let src = r#"{"a":1,"b":2,"c":3,"d":4}"#;

    let mut bare: serde_json::Map<String, serde_json::Value> =
        serde_json::from_str(src).unwrap();
    bare.remove("b");
    assert_eq!(
        bare.keys().collect::<Vec<_>>(),
        vec!["a", "d", "c"],
        "bare `remove` is `swap_remove` under preserve_order — this is the \
         trap, asserted so it is documented rather than assumed"
    );

    let mut shifted: serde_json::Map<String, serde_json::Value> =
        serde_json::from_str(src).unwrap();
    shifted.shift_remove("b");
    assert_eq!(
        shifted.keys().collect::<Vec<_>>(),
        vec!["a", "c", "d"],
        "`shift_remove` is the one that leaves survivors in place"
    );
}

/// Inserting over an EXISTING key updates in place; a genuinely new key is
/// appended. Together these are what make a read-modify-write a minimal
/// edit rather than a reshuffle.
#[test]
fn behaviour_insert_updates_in_place_and_appends_only_new_keys() {
    let mut v: serde_json::Value = serde_json::from_str(r#"{"first":1,"second":2}"#).unwrap();
    let obj = v.as_object_mut().unwrap();
    obj.insert("first".to_string(), serde_json::json!(99));
    obj.insert("third".to_string(), serde_json::json!(3));
    assert_eq!(
        obj.keys().collect::<Vec<_>>(),
        vec!["first", "second", "third"]
    );
}
