// SPDX-License-Identifier: AGPL-3.0-or-later
//! Cross-language parity: the Rust migration set vs `vco_lib/schema_versions.json`.
//!
//! ## Why this exists (v0.2.92, R16 + R24)
//!
//! `vco_lib/schema_versions.py` and `scripts/regen_schema_versions_json.py`
//! both stated that the generated JSON "is consumed by Rust at compile time
//! (`include_str!`)". It was not: `grep -rn schema_versions.json --include=*.rs`
//! returned nothing. The file's only readers were Python.
//!
//! Two ways to resolve a promise like that — make it true, or delete it. Here
//! making it true is worth more than the two lines it costs, because the claim
//! describes a gate that this very release needed: `LAUNCHER_DB_TABLE_SET_VERSION`
//! sat at 42 while `migrations.rs` had already registered
//! `043_chat_model_context.sql`, and the only thing that noticed was a Python
//! test. Anyone working purely on the Rust side — adding a migration, running
//! `cargo test`, seeing green — got no signal at all.
//!
//! So the parity is now enforced from BOTH sides against ONE committed
//! snapshot. Neither language can bump alone:
//!
//!   * add a migration without bumping the Python constant  -> this test fails;
//!   * bump the Python constant without adding the migration -> this test fails;
//!   * bump the constant and forget to regen the JSON        -> this test fails
//!     (and so does `regen_schema_versions_json.py --check`).
//!
//! ## Why `include_str!` rather than reading the file at runtime
//!
//! `include_str!` resolves relative to THIS source file, at COMPILE time, so
//! the test cannot silently pass by failing to find the JSON — a missing or
//! moved file is a build error naming the path. A runtime `read_to_string`
//! would have to decide what to do on `Err`, and the tempting answer (skip) is
//! exactly how a gate stops gating.

/// The committed snapshot, embedded at compile time.
/// Path is relative to this file: `launcher/src-tauri/tests/` -> repo root.
const SCHEMA_VERSIONS_JSON: &str = include_str!("../../../vco_lib/schema_versions.json");

/// `migrations.rs` itself, also embedded at compile time.
///
/// `MIGRATIONS` is a private `const` in `vct_launcher_core::db::migrations`
/// and this is an integration test (a separate crate), so the array is not
/// reachable as a value. Widening it to `pub` purely to let a test read it
/// would export an internal registry as public API — a worse trade than
/// parsing the source, which `include_str!` makes a COMPILE-TIME dependency:
/// if the file moves or is renamed, this fails to build with the path in the
/// error rather than silently skipping.
///
/// The parse is guarded below (`the_migration_parse_is_not_vacuous`) so a
/// formatting change that breaks the regex-free scan fails loudly instead of
/// reporting a plausible-looking wrong maximum.
const MIGRATIONS_RS: &str =
    include_str!("../vct-launcher-core/src/db/migrations.rs");

/// Every `version: N,` inside the `MIGRATIONS` array literal.
///
/// Bounded to the array (up to its closing `];`) so `SELF_TRANSACTIONAL_
/// MIGRATIONS` and any test fixtures further down the file cannot contribute.
fn registered_migration_versions() -> Vec<u32> {
    let start = MIGRATIONS_RS
        .find("const MIGRATIONS:")
        .expect("MIGRATIONS array not found in migrations.rs");
    let rest = &MIGRATIONS_RS[start..];
    let end = rest.find("];").map(|e| start + e).unwrap_or(MIGRATIONS_RS.len());
    let body = &MIGRATIONS_RS[start..end];

    let mut out = Vec::new();
    for (idx, _) in body.match_indices("version:") {
        let digits: String = body[idx + "version:".len()..]
            .chars()
            .skip_while(|c| c.is_whitespace())
            .take_while(|c| c.is_ascii_digit())
            .collect();
        if let Ok(v) = digits.parse::<u32>() {
            out.push(v);
        }
    }
    out
}

fn highest_registered_migration() -> u32 {
    let versions = registered_migration_versions();
    assert!(
        !versions.is_empty(),
        "no `version: N,` entries parsed out of the MIGRATIONS array — the \
         parse is broken, and a broken parse must fail rather than report a \
         maximum nobody registered"
    );
    *versions.iter().max().expect("non-empty")
}

fn launcher_db_table_set_from_json() -> u32 {
    let doc: serde_json::Value =
        serde_json::from_str(SCHEMA_VERSIONS_JSON).expect("schema_versions.json must be valid JSON");
    let versions = doc
        .get("canonical_versions")
        .and_then(|v| v.as_object())
        .expect("schema_versions.json must carry a `canonical_versions` object");
    versions
        .get("launcher_db_table_set")
        .and_then(|v| v.as_u64())
        .expect(
            "schema_versions.json must carry \
             canonical_versions.launcher_db_table_set",
        ) as u32
}

#[test]
fn launcher_db_table_set_version_matches_the_highest_registered_migration() {
    let from_json = launcher_db_table_set_from_json();
    let from_rust = highest_registered_migration();
    assert_eq!(
        from_json, from_rust,
        "`canonical_versions.launcher_db_table_set` = {from_json} in \
         vco_lib/schema_versions.json, but the highest migration registered in \
         vct-launcher-core/src/db/migrations.rs is {from_rust}.\n\n\
         A migration and its schema-version constant must be bumped ATOMICALLY \
         — a stamped version that is ahead of the applied set (or behind it) \
         makes every downstream recreate/upgrade decision wrong.\n\n\
         Fix: set LAUNCHER_DB_TABLE_SET_VERSION = {from_rust} in \
         vco_lib/schema_versions.py (with a `#: {from_rust} = …` note in the \
         existing per-version style), then run \
         `python scripts/regen_schema_versions_json.py`."
    );
}

/// Migration versions must be a dense 1..=N run with no duplicates. The
/// version is the runner's "have I applied this?" key, so a gap silently
/// skips schema and a duplicate silently shadows one.
#[test]
fn migration_versions_are_dense_and_unique() {
    let mut versions = registered_migration_versions();
    let declared = versions.len();
    versions.sort_unstable();
    versions.dedup();
    assert_eq!(
        versions.len(),
        declared,
        "duplicate migration version(s) in MIGRATIONS: {versions:?}"
    );
    let expected: Vec<u32> = (1..=declared as u32).collect();
    assert_eq!(
        versions, expected,
        "migration versions must run 1..={declared} with no gaps"
    );
}

/// Guard for the guard, half 1: the source scan must actually find the
/// registry. A parse that silently matched nothing would make both assertions
/// above vacuous, and this repo has already shipped a scanner that did exactly
/// that.
#[test]
fn the_migration_parse_is_not_vacuous() {
    let versions = registered_migration_versions();
    assert!(
        versions.len() >= 40,
        "parsed only {} migration versions out of migrations.rs — the array \
         has been growing since v0.2.x and cannot plausibly be this short. \
         Either the `version: N,` shape changed or the array bounds moved; \
         fix the parse rather than lowering this floor.",
        versions.len()
    );
    assert!(
        versions.contains(&1),
        "the initial migration (version 1) must be in the parsed set: {versions:?}"
    );
    // The scan must be bounded to the array: `SELF_TRANSACTIONAL_MIGRATIONS`
    // and the runner's own code live after `];` and must not contribute.
    assert!(
        versions.iter().all(|v| *v <= 500),
        "implausible version parsed — the scan escaped the MIGRATIONS array: \
         {versions:?}"
    );
}

/// Guard for the guard, half 2: prove the embedded JSON is the real, populated
/// snapshot and not an empty/placeholder file that would make both assertions
/// above vacuously true.
#[test]
fn the_embedded_snapshot_is_the_real_generated_file() {
    let doc: serde_json::Value =
        serde_json::from_str(SCHEMA_VERSIONS_JSON).expect("valid JSON");
    let versions = doc
        .get("canonical_versions")
        .and_then(|v| v.as_object())
        .expect("canonical_versions object");
    assert!(
        versions.len() > 10,
        "expected the full canonical version registry, got {} entries — is \
         this the generated file?",
        versions.len()
    );
    for key in ["kg_collection", "codegraph_collection", "launcher_db_table_set"] {
        assert!(
            versions.contains_key(key),
            "canonical_versions is missing `{key}`"
        );
    }
    assert!(
        doc.get("state_classification")
            .and_then(|v| v.as_object())
            .map(|o| !o.is_empty())
            .unwrap_or(false),
        "state_classification must be present and non-empty"
    );
}
