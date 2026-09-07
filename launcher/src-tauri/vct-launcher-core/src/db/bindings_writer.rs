// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Single-writer home for the launcher.db binding tables (X-1 / v0.2.76).
//!
//! Single-writer contract
//! ----------------------
//! The Rust launcher is the authoritative CREATOR of `project_kg_bindings` /
//! `project_codegraph_bindings` rows (the Python side only ever HEALS them,
//! via `vco_lib.kg_binding_heal` — see that module's matching contract
//! header). On the Rust side the base upsert SQL lives in
//! [`crate::db::project_state`]'s `Db::set_project_kg_binding` /
//! `Db::set_project_codegraph_binding` methods, and the drift-repair SQL lives
//! in [`crate::db::access`]. This module is the ONE place that owns the
//! **derive-a-name-then-write** orchestration: callers that need to seed a
//! project's default bindings hand a project NAME here, and the derivation
//! (via [`crate::db::access::sanitize_kg_collection_local`], the core-crate
//! copy of the shared Python-parity sanitizer) happens in one spot rather than
//! being open-coded at each call site.
//!
//! Enforcement: `tests/test_kg_binding_single_writer_rust.py` scans the Rust
//! sources and fails if any file OUTSIDE the allowlist
//! (`bindings_writer.rs`, `project_state.rs`, `access.rs`, `migrations.rs`)
//! issues a direct `INSERT`/`UPDATE` against the two binding tables. So a new
//! caller cannot quietly open-code a write — it must route through the
//! canonical `Db` methods (ideally via this module's helpers).
//!
//! Why not physically move every write here? The `project_state` upserts and
//! the `access` heal carry intricate lock/transaction context; hoisting the
//! raw SQL would be a large, risky change for no behaviour gain. The contract
//! is enforced by the allowlist lint + this documented entry point, matching
//! the S-M sizing in the X-1 design (`DESIGN-part9-gated-themes` §2).

use chrono::Utc;
use serde_json::Value as JsonValue;

use crate::db::project_state::{ProjectCodegraphBinding, ProjectKgBinding};
use crate::db::Db;

/// The canonical suffix appended to the sanitized project name to form the
/// per-project primary KG collection name.
pub const KG_PRIMARY_SUFFIX: &str = "_KnowledgeGraph";

/// Seed (or upsert) a project's PRIMARY KG binding, deriving the collection
/// name from `project_name` via the shared sanitizer. This is the ONE place
/// the KG-name derivation and the KG-binding write are wired together.
///
/// The KG-name sanitizer is [`crate::db::access::sanitize_kg_collection_local`]
/// — byte-equivalent to the launcher-crate `sanitize_kg_collection` and pinned
/// against the Python SSOT (`vco_lib.codegraph_naming.sanitize_for_weaviate_class`)
/// by `tests/fixtures/kg_sanitizer_parity.json`. Callers pass the raw name; the
/// derived basename + `_KnowledgeGraph` suffix is written.
#[allow(clippy::too_many_arguments)]
pub fn write_kg_binding_primary_from_name(
    db: &Db,
    project_id: &str,
    project_name: &str,
    embedding_model: Option<&str>,
    embedding_dim: Option<i64>,
    weaviate_url: Option<&str>,
    config: &JsonValue,
) -> Result<ProjectKgBinding, String> {
    let basename = crate::db::access::sanitize_kg_collection_local(project_name);
    let collection = format!("{basename}{KG_PRIMARY_SUFFIX}");
    db.set_project_kg_binding(
        project_id,
        "primary",
        &collection,
        embedding_model,
        embedding_dim,
        None,
        weaviate_url,
        config,
    )
}

/// Write a KG binding with an explicit collection name (no derivation) — the
/// thin routing seam for callers that already hold the resolved collection
/// name (e.g. the "shared" role pointing at the fixed shared collection). Kept
/// here so ALL binding-creation call sites can name a single writer module.
#[allow(clippy::too_many_arguments)]
pub fn write_kg_binding(
    db: &Db,
    project_id: &str,
    role: &str,
    collection_name: &str,
    embedding_model: Option<&str>,
    embedding_dim: Option<i64>,
    kg_dir_path: Option<&str>,
    weaviate_url: Option<&str>,
    config: &JsonValue,
) -> Result<ProjectKgBinding, String> {
    db.set_project_kg_binding(
        project_id,
        role,
        collection_name,
        embedding_model,
        embedding_dim,
        kg_dir_path,
        weaviate_url,
        config,
    )
}

// v0.2.76 (R2): `write_codegraph_binding_from_name` was DELETED. It derived the
// code-graph collection prefix via the KG-name sanitizer
// (`sanitize_kg_collection_local`, underscore-DROPPING), which is the WRONG rule
// for code-graph collections — the analyzer stamps them with the
// underscore-PRESERVING `canonical_class_prefix`. Its only caller
// (`populate_codegraph_binding`) now derives via `canonical_class_prefix` and
// routes through `write_codegraph_binding` (explicit prefix) below. Do NOT
// reintroduce a from_name helper for the codegraph table: the KG sanitizer must
// never derive a codegraph prefix (that is the R2 bug).

/// Write a code-graph binding with an explicit, already-resolved prefix (no
/// derivation) — the routing seam for callers that carry the prefix (e.g. the
/// rename-propagation path, which derives the prefix with `canonical_class_prefix`).
#[allow(clippy::too_many_arguments)]
pub fn write_codegraph_binding(
    db: &Db,
    project_id: &str,
    collection_prefix: &str,
    embedding_model: Option<&str>,
    embedding_dim: Option<i64>,
    last_analyzed_commit: Option<&str>,
    last_analyzed_at: Option<i64>,
    enabled: bool,
    config: &JsonValue,
) -> Result<ProjectCodegraphBinding, String> {
    db.set_project_codegraph_binding(
        project_id,
        collection_prefix,
        embedding_model,
        embedding_dim,
        last_analyzed_commit,
        last_analyzed_at,
        enabled,
        config,
    )
}

// ═══════════════════════════════════════════════════════════════════════════
// Path ancestry — the component-wise test both fixers below need
// ═══════════════════════════════════════════════════════════════════════════
//
// MUST MATCH `vco_lib/project_move.py`'s `is_ancestor` / `path_compare_key`
// pair, which is the tested Python home for this rule:
//
//   * component-wise, never `starts_with` on the raw string — `/a/proj` does
//     NOT contain `/a/project`, and a prefix test says it does;
//   * case-insensitive on Windows and macOS, case-SENSITIVE on Linux. macOS
//     needs the explicit fold: it is a case-insensitive filesystem that the
//     Rust/POSIX APIs treat as case-sensitive, so relying on the platform to
//     do it silently makes macOS behave like Linux.
//   * `.` and `..` COLLAPSE lexically, exactly as `os.path.normpath` (POSIX)
//     and `ntpath.normpath` (Windows) do inside `path_compare_key`. `..`
//     cannot climb above an absolute root or a drive letter; on a RELATIVE
//     path a leading `..` survives as a component, because `../x` genuinely
//     is "one above here". See `path_parts` for the case table.
//
// A cross-language mirror is tier C in the house rules (shared code > shared
// config > mirror). It is tier C here for a stated reason: the caller is a
// single SQL statement inside a transaction that already holds the SQLite
// lock, and shelling out to Python per row would turn a microsecond decision
// into a process spawn while holding a database lock.
//
// The divergence risk is bounded by `tests/fixtures/path_ancestry_parity.json`
// — a shared corpus of `(shape, path) -> components` and
// `(shape, ancestor, descendant) -> bool` vectors that BOTH implementations
// assert against: Rust in `path_ancestry_matches_shared_fixture` below,
// Python in `tests/test_v0292_wp18_rename_delivery.py
// ::TestPathAncestryParity`. A one-sided edit therefore fails on BOTH sides.
//
// Read that fixture's `_comment` before touching either implementation: it
// records what the corpus does NOT pin (Unicode case folding, backslashes
// used as literal filename characters on POSIX, Windows drive-RELATIVE paths
// such as `C:a`) and why. Before v0.2.92 MAJOR-12 the comment here claimed a
// parity test that did not exist — the named file asserted only that the
// string `fn is_ancestor` appeared in this source, which a comment satisfies,
// while `..` really did diverge: Python said `/a/b/../..` is not under `/a`,
// Rust said it is.

/// Comparison-normalised path components for `path`.
///
/// `case_insensitive` is a parameter rather than a `cfg!` so the Windows and
/// macOS SHAPES are unit-testable from Linux — R14 forbids "only verifiable
/// on <one OS>" as an acceptance criterion, and this repo's CI has one OS.
///
/// `..` is collapsed lexically, matching the Python home. The cases:
///
/// | input          | output       | why                                    |
/// |----------------|--------------|----------------------------------------|
/// | `/a/b/../c`    | `[a, c]`     | ordinary climb                         |
/// | `/a/b/../..`   | `[]`         | back at the root — NOT under `/a`      |
/// | `/../a`        | `[a]`        | nothing above an absolute root         |
/// | `a/../../b`    | `[.., b]`    | relative: `..` above here is real      |
/// | `C:\a\..\..`   | `[c:]`       | nothing above a drive letter           |
///
/// Callers that need the components with the user's ORIGINAL casing pass
/// `case_insensitive = false`; the `..` collapsing is identical either way,
/// which is what keeps a rebuilt path in step with the compare key.
fn path_parts(path: &str, case_insensitive: bool) -> Vec<String> {
    /// `C:`-shaped component — a Windows drive root that `..` cannot pop.
    fn is_drive(component: &str) -> bool {
        let b = component.as_bytes();
        b.len() == 2 && b[1] == b':' && b[0].is_ascii_alphabetic()
    }

    let mut components = path.split(['/', '\\']).filter(|p| !p.is_empty() && *p != ".");
    let mut out: Vec<String> = Vec::new();
    // Number of leading components `..` may never pop. A drive letter is a
    // root just as `/` is; `ntpath.normpath("C:\\..\\x")` is `C:\\x`.
    let mut floor = 0usize;
    // Rooted paths silently DROP a `..` that would escape; relative paths keep
    // it. Matches `normpath("/../a") == "/a"` vs `normpath("../a") == "../a"`.
    let mut rooted = path.starts_with('/') || path.starts_with('\\');

    if let Some(first) = components.next() {
        if is_drive(first) {
            rooted = true;
            floor = 1;
        }
        // Re-feed the first component through the same arm as the rest.
        for raw in std::iter::once(first).chain(components) {
            if raw == ".." {
                if out.len() > floor && out.last().map(|l| l != "..").unwrap_or(false) {
                    out.pop();
                } else if !rooted {
                    out.push("..".to_string());
                }
                continue;
            }
            out.push(if case_insensitive {
                raw.to_lowercase()
            } else {
                raw.to_string()
            });
        }
    }
    out
}

/// True when `ancestor` STRICTLY contains `descendant` (component-wise).
fn is_ancestor(ancestor: &str, descendant: &str, case_insensitive: bool) -> bool {
    let a = path_parts(ancestor, case_insensitive);
    let d = path_parts(descendant, case_insensitive);
    a.len() < d.len() && d[..a.len()] == a[..]
}

/// Whether THIS host's filesystem should be treated as case-insensitive.
fn host_is_case_insensitive() -> bool {
    cfg!(target_os = "windows") || cfg!(target_os = "macos")
}

/// Re-point `kg_dir_path` from `old_root` to `new_root` for one project.
///
/// v0.2.92 W14, closing the gap W3 documented and deliberately did not fill.
/// W3 needed this column re-pointed by a project MOVE, but writing binding SQL
/// from `db/projects.rs` trips the single-writer lint — and it refused to
/// evade an architectural gate that exists because these rows have been
/// corrupted by ad-hoc writers before. This is the same fix, in the sanctioned
/// home, called from inside the move's existing commit transaction.
///
/// Conservative by construction, and both halves matter:
///
/// * a NULL value is left NULL — the column is optional and inventing a value
///   for a row that never had one is not a re-point;
/// * a value that is NOT under `old_root` is left EXACTLY as it is. It is
///   either already correct or a deliberate user pointer at a directory
///   outside the project, and rewriting it would be VCO deciding for the user.
///
/// Takes `&Connection` (not `&Db`) so the caller passes its open transaction:
/// the re-point must commit or roll back WITH the flip, never separately.
///
/// Returns the number of rows actually changed.
pub fn repoint_kg_dir_path(
    conn: &rusqlite::Connection,
    project_id: &str,
    old_root: &str,
    new_root: &str,
) -> Result<usize, String> {
    repoint_kg_dir_path_ci(conn, project_id, old_root, new_root, host_is_case_insensitive())
}

/// [`repoint_kg_dir_path`] with the case-sensitivity decision injected, so the
/// Windows and macOS shapes are testable on a Linux CI host.
pub fn repoint_kg_dir_path_ci(
    conn: &rusqlite::Connection,
    project_id: &str,
    old_root: &str,
    new_root: &str,
    case_insensitive: bool,
) -> Result<usize, String> {
    let rows: Vec<(String, String)> = {
        let mut stmt = conn
            .prepare(
                "SELECT role, kg_dir_path FROM project_kg_bindings \
                 WHERE project_id = ?1 AND kg_dir_path IS NOT NULL",
            )
            .map_err(|e| format!("repoint_kg_dir_path prepare: {}", e))?;
        let mapped = stmt
            .query_map(rusqlite::params![project_id], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| format!("repoint_kg_dir_path query: {}", e))?;
        let mut out = Vec::new();
        for row in mapped {
            out.push(row.map_err(|e| format!("repoint_kg_dir_path row: {}", e))?);
        }
        out
    };

    let now = Utc::now().timestamp_millis();
    let mut changed = 0usize;
    for (role, current) in rows {
        // Equal to the old root, or strictly under it. Anything else is left
        // alone — that is the leave-alone half of the contract.
        let same = path_parts(&current, case_insensitive)
            == path_parts(old_root, case_insensitive);
        if !same && !is_ancestor(old_root, &current, case_insensitive) {
            continue;
        }
        let suffix_len = path_parts(old_root, case_insensitive).len();
        // Rebuild from the ORIGINAL components (not the case-folded compare
        // key) so a re-pointed path keeps the user's own casing below the root.
        // Same splitter, so `suffix_len` indexes the same sequence: computing
        // this list with a DIFFERENT `.`/`..` rule than the compare key would
        // slice at the wrong offset the moment a stored path contained `..`.
        let original = path_parts(&current, false);
        let tail = original[suffix_len.min(original.len())..].join("/");
        let new_value = if tail.is_empty() {
            new_root.to_string()
        } else {
            format!("{}/{}", new_root.trim_end_matches(['/', '\\']), tail)
        };
        if new_value == current {
            continue;
        }
        conn.execute(
            "UPDATE project_kg_bindings SET kg_dir_path = ?1, updated_at = ?2 \
             WHERE project_id = ?3 AND role = ?4",
            rusqlite::params![new_value, now, project_id, role],
        )
        .map_err(|e| format!("repoint_kg_dir_path update: {}", e))?;
        changed += 1;
    }
    Ok(changed)
}

// ═══════════════════════════════════════════════════════════════════════════
// Collection rename (v0.2.92 WP-18 / W14) — THE DURABLE COMMIT POINT
// ═══════════════════════════════════════════════════════════════════════════

/// What [`commit_collection_rename`] changed.
#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct CollectionRenameReport {
    pub project_id: String,
    pub new_name: String,
    pub new_slug: String,
    /// KG binding rows whose `collection_name` moved.
    pub kg_bindings_flipped: usize,
    /// 1 when the code-graph binding prefix moved, 0 when there was no row.
    pub codegraph_flipped: usize,
}

/// THE FLIP. One transaction: project name + slug, every KG binding row named
/// in `kg_bindings`, and the code-graph binding prefix.
///
/// Why this is one statement group and not three calls: a rename that moved
/// the code prefix while the KG binding stayed put is the EXACT pre-v0.2.89
/// defect (the field 'HouseOfFlirt' phantom) that made rename identity-
/// preserving in the first place. Splitting these across separate
/// transactions re-creates it the first time one of them fails.
///
/// What this does NOT do, deliberately:
///
/// * it does not touch Weaviate. The copy already happened and was verified;
///   by the time this runs the destination classes exist and hold the data.
///   No transaction spans both systems, so the ordering (copy, verify, THEN
///   flip) is what makes the pair safe — not a claim of atomicity.
/// * it does not drop anything, here or anywhere.
/// * it does not touch `kg_dir_path`. A rename does not move the project
///   folder, so re-pointing a folder path during one would be wrong; the
///   [`repoint_kg_dir_path`] fixer above belongs to the MOVE.
///
/// `expected_current_name` is a compare-and-swap guard: the caller planned
/// against a project row it read earlier, and if the name changed underneath
/// (a concurrent GUI rename) the plan's derived collection names no longer
/// describe this project. Refusing beats flipping to a family nobody computed.
pub fn commit_collection_rename(
    db: &Db,
    project_id: &str,
    expected_current_name: &str,
    new_name: &str,
    new_slug: &str,
    kg_bindings: &[(String, String)],
    codegraph_prefix: Option<&str>,
) -> Result<CollectionRenameReport, String> {
    let now = Utc::now().timestamp_millis();
    let report = {
        let mut guard = db.lock();
        let tx = guard
            .transaction()
            .map_err(|e| format!("commit_collection_rename begin: {}", e))?;

        let current: String = tx
            .query_row(
                "SELECT name FROM projects WHERE id = ?1",
                rusqlite::params![project_id],
                |r| r.get(0),
            )
            .map_err(|e| format!("commit_collection_rename read project: {}", e))?;
        if current != expected_current_name {
            return Err(format!(
                "project {} is now named '{}', not '{}' — the rename was \
                 planned against a different name and its derived collection \
                 names would not match. Nothing was changed.",
                project_id, current, expected_current_name
            ));
        }

        tx.execute(
            "UPDATE projects SET name = ?1, slug = ?2 WHERE id = ?3",
            rusqlite::params![new_name, new_slug, project_id],
        )
        .map_err(|e| format!("commit_collection_rename rename: {}", e))?;

        let mut kg_flipped = 0usize;
        for (role, collection) in kg_bindings {
            let n = tx
                .execute(
                    "UPDATE project_kg_bindings \
                     SET collection_name = ?1, updated_at = ?2 \
                     WHERE project_id = ?3 AND role = ?4",
                    rusqlite::params![collection, now, project_id, role],
                )
                .map_err(|e| format!("commit_collection_rename kg binding: {}", e))?;
            kg_flipped += n;
        }

        let mut cg_flipped = 0usize;
        if let Some(prefix) = codegraph_prefix {
            cg_flipped = tx
                .execute(
                    "UPDATE project_codegraph_bindings \
                     SET collection_prefix = ?1, updated_at = ?2 \
                     WHERE project_id = ?3",
                    rusqlite::params![prefix, now, project_id],
                )
                .map_err(|e| {
                    format!("commit_collection_rename codegraph binding: {}", e)
                })?;
        }

        tx.commit()
            .map_err(|e| format!("commit_collection_rename commit: {}", e))?;

        CollectionRenameReport {
            project_id: project_id.to_string(),
            new_name: new_name.to_string(),
            new_slug: new_slug.to_string(),
            kg_bindings_flipped: kg_flipped,
            codegraph_flipped: cg_flipped,
        }
    };

    // Observability AFTER the transaction and after the lock is released
    // (both of these take the lock themselves). An audit hiccup must never
    // roll back a completed rename.
    let _ = db.audit(
        "project_collection_rename",
        Some(project_id),
        None,
        &serde_json::json!({
            "old_name": expected_current_name,
            "new_name": new_name,
            "new_slug": new_slug,
            "kg_bindings_flipped": report.kg_bindings_flipped,
            "codegraph_flipped": report.codegraph_flipped,
        }),
    );
    let _ = db.log_change("projects", "update", Some(project_id), Some(project_id));
    Ok(report)
}

// ═══════════════════════════════════════════════════════════════════════════
// Tests — both sides of every destructive-capable step (v0.2.92 W14)
// ═══════════════════════════════════════════════════════════════════════════
//
// The leave-alone side is the one that matters. A suite that only proves the
// intended rows changed cannot tell a correct flip from one that also
// clobbered a neighbour, and a partial write is the failure this package
// exists to prevent.

#[cfg(test)]
mod rename_tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn db() -> Db {
        Db::open_in_memory().expect("in-memory db")
    }

    fn seed(db: &Db, id: &str, name: &str) {
        db.insert_project(id, name, &format!("/tmp/{id}"), ProjectHost::Base, id)
            .expect("insert project");
    }

    fn kg_row(db: &Db, project: &str, role: &str, collection: &str, dir: Option<&str>) {
        db.set_project_kg_binding(
            project,
            role,
            collection,
            None,
            None,
            dir,
            None,
            &serde_json::json!({}),
        )
        .expect("kg binding");
    }

    fn kg_name(db: &Db, project: &str, role: &str) -> String {
        let guard = db.lock();
        guard
            .query_row(
                "SELECT collection_name FROM project_kg_bindings \
                 WHERE project_id = ?1 AND role = ?2",
                rusqlite::params![project, role],
                |r| r.get(0),
            )
            .expect("read kg binding")
    }

    fn kg_dir(db: &Db, project: &str, role: &str) -> Option<String> {
        let guard = db.lock();
        guard
            .query_row(
                "SELECT kg_dir_path FROM project_kg_bindings \
                 WHERE project_id = ?1 AND role = ?2",
                rusqlite::params![project, role],
                |r| r.get(0),
            )
            .expect("read kg dir")
    }

    // ── ancestry ────────────────────────────────────────────────────────

    #[test]
    fn ancestry_is_component_wise_not_a_string_prefix() {
        // THE trap: `/a/proj` is a string prefix of `/a/project` and is not
        // its ancestor. A `starts_with` test re-points a sibling project's row.
        assert!(!is_ancestor("/a/proj", "/a/project", false));
        assert!(is_ancestor("/a/proj", "/a/proj/kg", false));
        assert!(!is_ancestor("/a/proj", "/a/proj", false), "strict, not equal");
    }

    #[test]
    fn ancestry_case_sensitivity_follows_the_host_shape() {
        // Linux: distinct paths. Windows/macOS: the same path.
        assert!(!is_ancestor("/A/Proj", "/a/proj/kg", false));
        assert!(is_ancestor("/A/Proj", "/a/proj/kg", true));
    }

    #[test]
    fn ancestry_accepts_windows_separators() {
        assert!(is_ancestor(r"C:\Proj", r"C:\Proj\kg", true));
        assert!(!is_ancestor(r"C:\Proj", r"C:\Project\kg", true));
    }

    #[test]
    fn dot_dot_cannot_climb_above_a_root() {
        // THE v0.2.92 MAJOR-12 divergence: this mirror used to keep `..` as an
        // ordinary component, so `/a/b/../..` looked like a 4-component path
        // "under" `/a` — and `repoint_kg_dir_path` would have rewritten that
        // row. Python's `normpath` says it is `/`, which is not under `/a`.
        assert!(!is_ancestor("/a", "/a/b/../..", false));
        assert_eq!(path_parts("/a/b/../..", false), Vec::<String>::new());
        assert_eq!(path_parts("/../a", false), vec!["a".to_string()]);
        // Relative: `..` above HERE is a real place, so it survives.
        assert_eq!(
            path_parts("a/../../b", false),
            vec!["..".to_string(), "b".to_string()]
        );
        // A drive letter is a root too.
        assert_eq!(path_parts(r"C:\a\b\..\..", true), vec!["c:".to_string()]);
        assert_eq!(
            path_parts(r"C:\..\x", true),
            vec!["c:".to_string(), "x".to_string()]
        );
    }

    // ── the shared cross-language corpus ────────────────────────────────
    //
    // `tests/fixtures/path_ancestry_parity.json` is the table the tier-C
    // mirror comment at the top of this file claims bounds the divergence
    // risk. Before v0.2.92 MAJOR-12 that claim was false: the named Python
    // file asserted only that the strings `fn is_ancestor` and `MUST MATCH`
    // appeared in this source, and `path_compare_key` had no test at all.
    //
    // Path resolution copies `commands/project_identity.rs`'s shape:
    // `CARGO_MANIFEST_DIR` (= `launcher/src-tauri/vct-launcher-core/`) walked
    // up THREE parents to the repo root. The bare `env!` is compile-time-only
    // and lives inside `#[cfg(test)]`, so it never ships in a release binary.
    //
    // An unreadable or unparsable fixture PANICS. A skip here would restore
    // exactly the silence this corpus exists to end.

    #[derive(serde::Deserialize)]
    struct PartsVector {
        shape: String,
        path: String,
        expected: Vec<String>,
    }

    #[derive(serde::Deserialize)]
    struct AncestryVector {
        shape: String,
        ancestor: String,
        descendant: String,
        expected: bool,
    }

    #[derive(serde::Deserialize)]
    struct AncestryFixture {
        parts: Vec<PartsVector>,
        ancestry: Vec<AncestryVector>,
    }

    /// Rust has ONE `path_parts` for three OSes; the corpus names the shape.
    /// `windows` and `posix-insensitive` both land on `case_insensitive =
    /// true` — they differ only in separator conventions, which `path_parts`
    /// is agnostic to (see the fixture's "does not pin" note).
    fn case_insensitive_for_shape(shape: &str) -> bool {
        match shape {
            "posix-sensitive" => false,
            "posix-insensitive" | "windows" => true,
            other => panic!(
                "unknown shape {:?} in tests/fixtures/path_ancestry_parity.json \
                 — add it to BOTH readers before adding vectors that use it",
                other
            ),
        }
    }

    fn load_path_ancestry_fixture() -> AncestryFixture {
        let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest_dir
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .expect("CARGO_MANIFEST_DIR must have three parents (repo layout)");
        let fixture_path = repo_root
            .join("tests")
            .join("fixtures")
            .join("path_ancestry_parity.json");
        let raw = std::fs::read_to_string(&fixture_path).unwrap_or_else(|e| {
            panic!(
                "read {}: {} -- shared with tests/test_v0292_wp18_rename_delivery.py\
                 ::TestPathAncestryParity",
                fixture_path.display(),
                e
            )
        });
        let fix: AncestryFixture = serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("parse {}: {}", fixture_path.display(), e));
        assert!(
            !fix.parts.is_empty() && !fix.ancestry.is_empty(),
            "Fixture {} has no vectors",
            fixture_path.display()
        );
        fix
    }

    #[test]
    fn path_ancestry_matches_shared_fixture() {
        let fix = load_path_ancestry_fixture();
        let mut failures: Vec<String> = Vec::new();

        for v in &fix.parts {
            let got = path_parts(&v.path, case_insensitive_for_shape(&v.shape));
            if got != v.expected {
                failures.push(format!(
                    "[{}] path_parts({:?}) = {:?}, fixture expects {:?}",
                    v.shape, v.path, got, v.expected
                ));
            }
        }
        for v in &fix.ancestry {
            let got = is_ancestor(
                &v.ancestor,
                &v.descendant,
                case_insensitive_for_shape(&v.shape),
            );
            if got != v.expected {
                failures.push(format!(
                    "[{}] is_ancestor({:?}, {:?}) = {}, fixture expects {}",
                    v.shape, v.ancestor, v.descendant, got, v.expected
                ));
            }
        }

        assert!(
            failures.is_empty(),
            "Rust mirror diverged from the shared parity corpus \
             (tests/fixtures/path_ancestry_parity.json), which \
             vco_lib/project_move.py asserts against too:\n{}",
            failures.join("\n")
        );
    }

    // ── repoint_kg_dir_path: act AND leave-alone ────────────────────────

    #[test]
    fn repoint_kg_dir_path_rebases_a_value_under_the_old_root() {
        let db = db();
        seed(&db, "p1", "One");
        kg_row(&db, "p1", "primary", "One_KnowledgeGraph", Some("/old/p1/knowledge"));
        let guard = db.lock();
        let n = repoint_kg_dir_path_ci(&guard, "p1", "/old/p1", "/new/p1", false)
            .expect("repoint");
        drop(guard);
        assert_eq!(n, 1);
        assert_eq!(kg_dir(&db, "p1", "primary").as_deref(), Some("/new/p1/knowledge"));
    }

    #[test]
    fn repoint_kg_dir_path_rebuilds_a_stored_path_that_contains_dot_dot() {
        // The rebuild slices the ORIGINAL-case components at an offset taken
        // from the compare-key components. Once `path_parts` collapses `..`,
        // an inline splitter that did NOT collapse would produce a longer
        // list and slice at the wrong place — `/old/p1/x/../knowledge` would
        // come back as `/new/p1/../knowledge` instead of `/new/p1/knowledge`.
        let db = db();
        seed(&db, "p1", "One");
        kg_row(
            &db,
            "p1",
            "primary",
            "One_KnowledgeGraph",
            Some("/old/p1/x/../knowledge"),
        );
        let guard = db.lock();
        let n = repoint_kg_dir_path_ci(&guard, "p1", "/old/p1", "/new/p1", false)
            .expect("repoint");
        drop(guard);
        assert_eq!(n, 1);
        assert_eq!(
            kg_dir(&db, "p1", "primary").as_deref(),
            Some("/new/p1/knowledge")
        );
    }

    #[test]
    fn repoint_kg_dir_path_leaves_a_dot_dot_path_that_escapes_the_root_alone() {
        // The leave-alone half, on the vector MAJOR-12 is named for:
        // `/old/p1/x/../..` IS `/old`, which is not under `/old/p1`. Before
        // the `..` fix this row was rewritten — a false-positive write on a
        // user-owned column.
        let db = db();
        seed(&db, "p1", "One");
        kg_row(&db, "p1", "primary", "One_KnowledgeGraph", Some("/old/p1/x/../.."));
        let guard = db.lock();
        let n = repoint_kg_dir_path_ci(&guard, "p1", "/old/p1", "/new/p1", false)
            .expect("repoint");
        drop(guard);
        assert_eq!(n, 0, "a path that resolves ABOVE the old root is leave-alone");
        assert_eq!(
            kg_dir(&db, "p1", "primary").as_deref(),
            Some("/old/p1/x/../..")
        );
    }

    #[test]
    fn repoint_kg_dir_path_leaves_a_value_outside_the_old_root_alone() {
        let db = db();
        seed(&db, "p1", "One");
        kg_row(&db, "p1", "primary", "One_KnowledgeGraph", Some("/elsewhere/kg"));
        let guard = db.lock();
        let n = repoint_kg_dir_path_ci(&guard, "p1", "/old/p1", "/new/p1", false)
            .expect("repoint");
        drop(guard);
        assert_eq!(n, 0, "a deliberate pointer outside the project is the user's");
        assert_eq!(kg_dir(&db, "p1", "primary").as_deref(), Some("/elsewhere/kg"));
    }

    #[test]
    fn repoint_kg_dir_path_leaves_a_sibling_prefix_path_alone() {
        // The component-wise rule, proven at the SQL level and not just in
        // the pure helper: `/old/proj` must not capture `/old/project`.
        let db = db();
        seed(&db, "p1", "One");
        kg_row(&db, "p1", "primary", "One_KnowledgeGraph", Some("/old/project/kg"));
        let guard = db.lock();
        let n = repoint_kg_dir_path_ci(&guard, "p1", "/old/proj", "/new/proj", false)
            .expect("repoint");
        drop(guard);
        assert_eq!(n, 0);
        assert_eq!(kg_dir(&db, "p1", "primary").as_deref(), Some("/old/project/kg"));
    }

    #[test]
    fn repoint_kg_dir_path_leaves_null_null() {
        let db = db();
        seed(&db, "p1", "One");
        kg_row(&db, "p1", "primary", "One_KnowledgeGraph", None);
        let guard = db.lock();
        let n = repoint_kg_dir_path_ci(&guard, "p1", "/old/p1", "/new/p1", false)
            .expect("repoint");
        drop(guard);
        assert_eq!(n, 0, "an optional column that was never set stays unset");
        assert_eq!(kg_dir(&db, "p1", "primary"), None);
    }

    #[test]
    fn repoint_kg_dir_path_never_touches_another_project() {
        let db = db();
        seed(&db, "p1", "One");
        seed(&db, "p2", "Two");
        kg_row(&db, "p1", "primary", "One_KnowledgeGraph", Some("/old/p1/kg"));
        kg_row(&db, "p2", "primary", "Two_KnowledgeGraph", Some("/old/p1/kg"));
        let guard = db.lock();
        repoint_kg_dir_path_ci(&guard, "p1", "/old/p1", "/new/p1", false)
            .expect("repoint");
        drop(guard);
        assert_eq!(kg_dir(&db, "p2", "primary").as_deref(), Some("/old/p1/kg"));
    }

    #[test]
    fn repoint_kg_dir_path_windows_case_variant_is_an_act() {
        let db = db();
        seed(&db, "p1", "One");
        kg_row(&db, "p1", "primary", "One_KnowledgeGraph", Some(r"C:\Proj\knowledge"));
        let guard = db.lock();
        let n = repoint_kg_dir_path_ci(&guard, "p1", r"c:\proj", r"D:\Moved", true)
            .expect("repoint");
        drop(guard);
        assert_eq!(n, 1, "on Windows a case variant IS the same path");
        assert_eq!(kg_dir(&db, "p1", "primary").as_deref(), Some(r"D:\Moved/knowledge"));
    }

    #[test]
    fn repoint_kg_dir_path_case_variant_is_a_leave_alone_on_linux() {
        let db = db();
        seed(&db, "p1", "One");
        kg_row(&db, "p1", "primary", "One_KnowledgeGraph", Some("/Proj/knowledge"));
        let guard = db.lock();
        let n = repoint_kg_dir_path_ci(&guard, "p1", "/proj", "/moved", false)
            .expect("repoint");
        drop(guard);
        assert_eq!(n, 0, "on Linux /Proj and /proj are different directories");
        assert_eq!(kg_dir(&db, "p1", "primary").as_deref(), Some("/Proj/knowledge"));
    }

    // ── commit_collection_rename: act AND leave-alone ───────────────────

    #[test]
    fn commit_flips_name_slug_and_both_binding_families() {
        let db = db();
        seed(&db, "p1", "Old Name");
        kg_row(&db, "p1", "primary", "OldName_KnowledgeGraph", None);
        db.set_project_codegraph_binding(
            "p1", "Old_Name", None, None, None, None, true, &serde_json::json!({}),
        )
        .expect("codegraph binding");

        let report = commit_collection_rename(
            &db,
            "p1",
            "Old Name",
            "New Name",
            "new-name",
            &[("primary".to_string(), "NewName_KnowledgeGraph".to_string())],
            Some("New_Name"),
        )
        .expect("commit");

        assert_eq!(report.kg_bindings_flipped, 1);
        assert_eq!(report.codegraph_flipped, 1);
        let row = db.get_project("p1").unwrap().unwrap();
        assert_eq!(row.name, "New Name");
        assert_eq!(row.slug, "new-name");
        assert_eq!(kg_name(&db, "p1", "primary"), "NewName_KnowledgeGraph");
        let guard = db.lock();
        let prefix: String = guard
            .query_row(
                "SELECT collection_prefix FROM project_codegraph_bindings \
                 WHERE project_id = 'p1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(prefix, "New_Name");
    }

    #[test]
    fn commit_leaves_every_other_project_untouched() {
        let db = db();
        seed(&db, "p1", "Old Name");
        seed(&db, "p2", "Peer");
        kg_row(&db, "p1", "primary", "OldName_KnowledgeGraph", None);
        kg_row(&db, "p2", "primary", "Peer_KnowledgeGraph", Some("/peer/kg"));

        commit_collection_rename(
            &db,
            "p1",
            "Old Name",
            "New Name",
            "new-name",
            &[("primary".to_string(), "NewName_KnowledgeGraph".to_string())],
            None,
        )
        .expect("commit");

        let peer = db.get_project("p2").unwrap().unwrap();
        assert_eq!(peer.name, "Peer");
        assert_eq!(kg_name(&db, "p2", "primary"), "Peer_KnowledgeGraph");
        assert_eq!(kg_dir(&db, "p2", "primary").as_deref(), Some("/peer/kg"));
    }

    #[test]
    fn commit_leaves_the_shared_role_alone_when_it_is_not_in_the_payload() {
        // The shared KG is install-owned. A project rename must never move
        // the name every OTHER project reads.
        let db = db();
        seed(&db, "p1", "Old Name");
        kg_row(&db, "p1", "primary", "OldName_KnowledgeGraph", None);
        kg_row(&db, "p1", "shared", "VibeCodedOrchestrator_KnowledgeGraph", None);

        commit_collection_rename(
            &db,
            "p1",
            "Old Name",
            "New Name",
            "new-name",
            &[("primary".to_string(), "NewName_KnowledgeGraph".to_string())],
            None,
        )
        .expect("commit");

        assert_eq!(
            kg_name(&db, "p1", "shared"),
            "VibeCodedOrchestrator_KnowledgeGraph"
        );
    }

    #[test]
    fn commit_refuses_when_the_name_changed_underneath_and_changes_nothing() {
        let db = db();
        seed(&db, "p1", "Actual Name");
        kg_row(&db, "p1", "primary", "ActualName_KnowledgeGraph", None);

        let err = commit_collection_rename(
            &db,
            "p1",
            "Stale Name",
            "New Name",
            "new-name",
            &[("primary".to_string(), "NewName_KnowledgeGraph".to_string())],
            Some("New_Name"),
        )
        .expect_err("must refuse a compare-and-swap miss");
        assert!(err.contains("planned against a different name"), "{err}");

        let row = db.get_project("p1").unwrap().unwrap();
        assert_eq!(row.name, "Actual Name", "leave-alone: nothing was written");
        assert_eq!(kg_name(&db, "p1", "primary"), "ActualName_KnowledgeGraph");
    }

    /// THE INTERRUPTION TEST, with the failure landing LAST.
    ///
    /// W3's first atomicity test passed for the wrong reason: its failure hit
    /// statement 1, so nothing partial existed to roll back and the test
    /// survived a red-proof mutation. This one drops the code-graph binding
    /// table so the failure lands on the LAST statement — AFTER the project
    /// rename and the KG binding update have both applied inside the
    /// transaction. If the transaction were not the unit of work, the project
    /// would be left renamed with its KG binding moved and its code prefix
    /// stale: a half-flipped identity, which is exactly the pre-v0.2.89 defect.
    #[test]
    fn commit_rolls_back_a_flip_that_already_succeeded_when_a_later_step_fails() {
        let db = db();
        seed(&db, "p1", "Old Name");
        kg_row(&db, "p1", "primary", "OldName_KnowledgeGraph", None);
        {
            let guard = db.lock();
            guard
                .execute("DROP TABLE project_codegraph_bindings", [])
                .expect("drop the table the LAST statement needs");
        }

        let err = commit_collection_rename(
            &db,
            "p1",
            "Old Name",
            "New Name",
            "new-name",
            &[("primary".to_string(), "NewName_KnowledgeGraph".to_string())],
            Some("New_Name"),
        )
        .expect_err("the codegraph update must fail");
        assert!(err.contains("codegraph binding"), "{err}");

        // Both EARLIER writes must be gone.
        let row = db.get_project("p1").unwrap().unwrap();
        assert_eq!(row.name, "Old Name", "the rename rolled back");
        assert_eq!(row.slug, "p1", "the slug rolled back");
        assert_eq!(
            kg_name(&db, "p1", "primary"),
            "OldName_KnowledgeGraph",
            "the KG binding rolled back"
        );
    }

    #[test]
    fn commit_with_no_codegraph_row_reports_zero_rather_than_failing() {
        // A project that has never been analyzed has no code-graph binding.
        // That is a normal state, not an error: the analyzer writes the row
        // on its first run, and it will write the NEW prefix.
        let db = db();
        seed(&db, "p1", "Old Name");
        kg_row(&db, "p1", "primary", "OldName_KnowledgeGraph", None);

        let report = commit_collection_rename(
            &db, "p1", "Old Name", "New Name", "new-name",
            &[("primary".to_string(), "NewName_KnowledgeGraph".to_string())],
            Some("New_Name"),
        )
        .expect("commit");
        assert_eq!(report.codegraph_flipped, 0);
        assert_eq!(report.kg_bindings_flipped, 1);
    }
}
