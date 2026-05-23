// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Module-shipped DB migration apply mechanism (v0.2.31).
//!
//! Modules ship a `db/` directory with SQL files named `[0-9]+_*.sql` and
//! declare a `db` block in their `vct-module.json` manifest:
//!
//! ```jsonc
//! "db": { "migrations_dir": "db/", "namespace": "rl" }
//! ```
//!
//! The launcher applies those files at install + update + manual repair
//! time via [`apply_module_db_migrations`]. The apply is idempotent
//! (SHA256-keyed) and namespace-scoped (every CREATE TABLE / ALTER TABLE /
//! CREATE INDEX is validated against the manifest's namespace prefix
//! before execution; FOREIGN KEY references to launcher-owned tables —
//! e.g. `projects(id)` — are explicitly allowed).
//!
//! ### Tracking
//!
//! Applied migrations are recorded in the launcher's own
//! `module_db_migrations` table (migration 019). Schema:
//!
//! ```sql
//! module_db_migrations (
//!     module_id  TEXT NOT NULL,
//!     filename   TEXT NOT NULL,
//!     sha256     TEXT NOT NULL,
//!     namespace  TEXT NOT NULL,
//!     applied_at INTEGER NOT NULL,
//!     PRIMARY KEY (module_id, filename)
//! )
//! ```
//!
//! ### Failure modes
//!
//! Three structured error kinds, each surfaced as a [`MigrationError`]
//! entry in the returned report:
//!
//! - [`MigrationErrorKind::ShaMismatch`] — the file has been mutated since
//!   it was last applied. Modules MUST NOT mutate shipped migrations;
//!   they ship new files (`0002_*.sql` instead of editing `0001_*.sql`).
//!   The fix is module-author homework, not user-side.
//! - [`MigrationErrorKind::NamespaceViolation`] — the SQL creates / alters
//!   a table outside `{namespace}_*`. The launcher's regex parser catches
//!   this BEFORE executing the SQL (rather than relying on a post-hoc
//!   `PRAGMA foreign_key_check` or table-name sweep).
//! - [`MigrationErrorKind::SqlExecutionFailed`] — SQLite itself rejected
//!   the SQL (syntax error, type mismatch, FK violation against an
//!   already-existing row, etc.). The error message includes the SQLite
//!   diagnostic.
//!
//! ### Soft-fail contract
//!
//! On error, the apply returns early — subsequent files in the same module
//! are NOT applied (we don't want a half-applied migration set to leave
//! the DB in an inconsistent state). The caller is expected to treat
//! errors as a soft-fail signal: the install / update completes, the
//! module is marked installed, but its DB-backed surfaces (`/rl_state`
//! reads, `/finetune_status` writes) return empty / error until the
//! migration error is resolved (via a NEW migration file or a manual
//! repair). The `UPDATE_DEFERRED.md` deferral surface in the install
//! flow records the failure so the user sees the actionable diagnostic
//! at next session start.

use std::path::{Path, PathBuf};

use chrono::Utc;
use rusqlite::params;
use sha2::{Digest, Sha256};

use crate::manifest::ModuleManifest;

use super::Db;

/// Per-file outcome of an apply pass.
#[derive(Debug, Clone, serde::Serialize)]
pub struct MigrationError {
    pub filename: String,
    pub kind: MigrationErrorKind,
    pub message: String,
}

/// Why a single migration file failed to apply.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MigrationErrorKind {
    /// SHA256 of the file on disk does NOT match the SHA256 we recorded
    /// at first-apply time. The module mutated a shipped migration —
    /// the fix is to ship a new file, not edit the old one. The
    /// launcher refuses to re-execute mutated SQL because re-running
    /// could double-apply schema changes that aren't idempotent.
    ShaMismatch,

    /// The SQL creates / alters / indexes a table outside the manifest's
    /// declared namespace prefix. Refused before execution.
    NamespaceViolation,

    /// SQLite raised an error while executing the statement. Could be
    /// syntax, type, FK, or any other DB-level diagnostic. The
    /// `message` field carries SQLite's text verbatim.
    SqlExecutionFailed,

    /// A different module has already shipped migrations with this same
    /// namespace prefix. Refusing prevents cross-module table collisions
    /// (e.g. module A's `rl_state` would be writable by module B's
    /// declared `rl` namespace if we didn't enforce uniqueness).
    NamespaceCollision,
}

/// Aggregate result of running `apply_module_db_migrations` for one
/// module's `db/` directory.
#[derive(Debug, Clone, serde::Serialize)]
pub struct MigrationReport {
    pub module_id: String,
    /// Filenames whose SQL was executed in this pass. New rows in
    /// `module_db_migrations` correspond 1:1.
    pub applied: Vec<String>,
    /// Filenames whose SHA matched a previously-applied row — skipped
    /// silently, no SQL ran.
    pub skipped: Vec<String>,
    /// Per-file failures. On the first error, the apply pass stops
    /// (subsequent files are not attempted). `errors.len() <= 1` in
    /// practice; the Vec shape is forward-compatible with future
    /// continue-on-error behavior.
    pub errors: Vec<MigrationError>,
}

impl MigrationReport {
    pub fn ok(&self) -> bool {
        self.errors.is_empty()
    }

    fn new(module_id: &str) -> Self {
        Self {
            module_id: module_id.to_string(),
            applied: Vec::new(),
            skipped: Vec::new(),
            errors: Vec::new(),
        }
    }

    fn push_error(&mut self, filename: &str, kind: MigrationErrorKind, message: String) {
        self.errors.push(MigrationError {
            filename: filename.to_string(),
            kind,
            message,
        });
    }
}

/// Apply every migration file the module ships in its `db/` directory.
///
/// `module_install_dir` is the absolute path the launcher resolved for
/// the module's install location (from `manifest.install.install_dir`).
/// The migrations dir is `module_install_dir.join(manifest.db.migrations_dir)`.
///
/// If `manifest.db` is `None`, returns an empty report with no errors —
/// the module declared zero DB needs.
///
/// On the first failure (sha mismatch, namespace violation, sql exec
/// failure, namespace collision), the apply pass stops and returns the
/// report with the error attached. Subsequent files are NOT attempted.
///
/// Caller pattern (install / update / manual-repair):
///
/// ```ignore
/// let report = apply_module_db_migrations(&db, "vct-rl-reranker", &install_dir, &manifest)?;
/// if !report.ok() {
///     eprintln!("[installer] {} migrations failed: {:?}", report.module_id, report.errors);
///     // Soft-fail: install completes anyway, deferral logged.
/// }
/// ```
pub fn apply_module_db_migrations(
    db: &Db,
    module_id: &str,
    module_install_dir: &Path,
    manifest: &ModuleManifest,
) -> Result<MigrationReport, String> {
    let mut report = MigrationReport::new(module_id);

    let db_block = match manifest.db.as_ref() {
        Some(b) => b,
        None => return Ok(report),
    };

    // Resolve + check the migrations dir exists. A manifest can declare
    // `db.migrations_dir` while shipping zero files (e.g. a future
    // version that drops a previously-needed table). We don't error
    // on "dir missing" — empty report is the right answer.
    let migrations_dir: PathBuf = module_install_dir.join(&db_block.migrations_dir);
    if !migrations_dir.is_dir() {
        return Ok(report);
    }

    // ─── Namespace collision soft check ─────────────────────────────────
    //
    // Refuse to apply if a DIFFERENT module has previously claimed the
    // same namespace prefix. Single index lookup against the
    // `idx_module_db_migrations_namespace` index. If we ever shipped
    // a paid module A with namespace="foo" and the user installs paid
    // module B that also declares namespace="foo", B's migrations
    // would overwrite / collide with A's tables. Refusing here is the
    // safe default; resolution is the publishers renaming their
    // namespace.
    {
        let guard = db.lock();
        let mut stmt = guard
            .prepare(
                "SELECT DISTINCT module_id FROM module_db_migrations \
                 WHERE namespace = ?1 AND module_id != ?2",
            )
            .map_err(|e| format!("prepare namespace-collision query: {}", e))?;
        let other_modules: Vec<String> = stmt
            .query_map(params![&db_block.namespace, module_id], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query namespace-collision: {}", e))?
            .filter_map(|r| r.ok())
            .collect();

        if !other_modules.is_empty() {
            // Synthetic "filename" since the collision is detected
            // pre-file-list; the message carries the actionable info.
            let conflict = other_modules.join(", ");
            report.push_error(
                "<namespace-precheck>",
                MigrationErrorKind::NamespaceCollision,
                format!(
                    "namespace '{}' is already claimed by module(s): [{}]. \
                     Two modules cannot share a namespace prefix — \
                     ask the module publisher to rename `db.namespace` \
                     in vct-module.json",
                    db_block.namespace, conflict,
                ),
            );
            return Ok(report);
        }
    }

    // ─── Discover + sort migration files ────────────────────────────────
    let mut files: Vec<PathBuf> = std::fs::read_dir(&migrations_dir)
        .map_err(|e| format!("read_dir {}: {}", migrations_dir.display(), e))?
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.path())
        .filter(|p| is_migration_file(p))
        .collect();

    // Standard-sort works because the filename convention is zero-padded
    // numeric prefix (`0001_foo.sql` < `0002_bar.sql`).
    files.sort();

    // ─── Apply each in order ────────────────────────────────────────────
    for path in files {
        let filename = match path.file_name().and_then(|s| s.to_str()) {
            Some(n) => n.to_string(),
            None => {
                // Non-UTF8 filename — extremely rare on modern systems.
                // Skip silently rather than failing the whole apply.
                eprintln!(
                    "[module_db_migrations] skipping non-UTF8 filename in {}",
                    migrations_dir.display()
                );
                continue;
            }
        };

        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(e) => {
                report.push_error(
                    &filename,
                    MigrationErrorKind::SqlExecutionFailed,
                    format!("read {}: {}", path.display(), e),
                );
                return Ok(report);
            }
        };
        let sha = sha256_hex(&bytes);

        let sql = match std::str::from_utf8(&bytes) {
            Ok(s) => s.to_string(),
            Err(e) => {
                report.push_error(
                    &filename,
                    MigrationErrorKind::SqlExecutionFailed,
                    format!("non-UTF8 SQL file: {}", e),
                );
                return Ok(report);
            }
        };

        // ── Idempotent skip / sha-mismatch refusal ────────────────────
        let existing: Option<String> = {
            let guard = db.lock();
            guard
                .query_row(
                    "SELECT sha256 FROM module_db_migrations \
                     WHERE module_id = ?1 AND filename = ?2",
                    params![module_id, &filename],
                    |row| row.get::<_, String>(0),
                )
                .ok()
        };

        if let Some(prev_sha) = existing {
            if prev_sha == sha {
                report.skipped.push(filename);
                continue;
            }
            report.push_error(
                &filename,
                MigrationErrorKind::ShaMismatch,
                format!(
                    "migration file '{}' was previously applied with a different SHA256. \
                     Modules MUST NOT mutate shipped migrations — ship a new file instead.",
                    filename
                ),
            );
            return Ok(report);
        }

        // ── Namespace-prefix validation ───────────────────────────────
        if let Err(msg) = validate_sql_namespace(&sql, &db_block.namespace) {
            report.push_error(
                &filename,
                MigrationErrorKind::NamespaceViolation,
                msg,
            );
            return Ok(report);
        }

        // ── Execute inside a transaction ──────────────────────────────
        let exec_result: Result<(), String> = (|| {
            let mut guard = db.lock();
            let tx = guard
                .transaction()
                .map_err(|e| format!("begin tx: {}", e))?;
            tx.execute_batch(&sql)
                .map_err(|e| format!("execute SQL: {}", e))?;
            tx.execute(
                "INSERT INTO module_db_migrations \
                    (module_id, filename, sha256, namespace, applied_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![
                    module_id,
                    &filename,
                    &sha,
                    &db_block.namespace,
                    Utc::now().timestamp_millis(),
                ],
            )
            .map_err(|e| format!("record applied migration: {}", e))?;
            tx.commit().map_err(|e| format!("commit tx: {}", e))?;
            Ok(())
        })();

        match exec_result {
            Ok(()) => {
                eprintln!(
                    "[module_db_migrations] applied {}::{} (sha={}..)",
                    module_id,
                    filename,
                    &sha[..16.min(sha.len())],
                );
                report.applied.push(filename);
            }
            Err(e) => {
                report.push_error(
                    &filename,
                    MigrationErrorKind::SqlExecutionFailed,
                    e,
                );
                return Ok(report);
            }
        }
    }

    Ok(report)
}

/// Whether a path looks like a migration file: matches `[0-9]+_*.sql`.
///
/// Examples that pass: `0001_initial.sql`, `42_add_index.sql`.
/// Examples that fail: `README.md`, `_skip.sql`, `setup.sql` (no digit prefix),
/// `0001_.sql` (no name after underscore? — actually passes; the constraint
/// is "starts with one-or-more digits then underscore", not "non-empty
/// suffix"; matches Postgres-style numeric-prefix convention).
fn is_migration_file(path: &Path) -> bool {
    let name = match path.file_name().and_then(|s| s.to_str()) {
        Some(n) => n,
        None => return false,
    };
    if !name.to_ascii_lowercase().ends_with(".sql") {
        return false;
    }
    // First char must be digit.
    let mut chars = name.chars();
    let first = match chars.next() {
        Some(c) => c,
        None => return false,
    };
    if !first.is_ascii_digit() {
        return false;
    }
    // Walk until we hit a non-digit. Must be '_'.
    let mut saw_underscore = false;
    for c in chars {
        if c.is_ascii_digit() {
            continue;
        }
        if c == '_' {
            saw_underscore = true;
        }
        break;
    }
    saw_underscore
}

/// Compute the lowercase-hex SHA256 of a byte slice.
fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}

/// Walk SQL DDL statements and assert every CREATE TABLE / ALTER TABLE /
/// CREATE INDEX (target table) starts with `{namespace}_`.
///
/// Returns Ok(()) when every detected DDL subject is namespaced;
/// Err with a descriptive message naming the offending statement
/// otherwise.
///
/// FOREIGN KEY references to non-namespaced tables (e.g. `projects(id)`)
/// are explicitly allowed — they're inside the CREATE TABLE body, not
/// the subject. The regex only looks at the CREATE / ALTER subject.
///
/// Best-effort regex parser (not a full SQL grammar). Handles:
/// - Case-insensitive keywords (`CREATE TABLE`, `create table`).
/// - `IF NOT EXISTS` between CREATE TABLE and the name.
/// - `TEMP` / `TEMPORARY` between CREATE and TABLE.
/// - Quoted identifiers (`"foo"`, `[foo]` style is NOT supported — SQLite
///   accepts `[foo]` for compatibility, but the standard is `"foo"`;
///   modules should use the standard).
/// - `CREATE UNIQUE INDEX` and `CREATE INDEX IF NOT EXISTS`.
///
/// Does NOT cover:
/// - `CREATE TRIGGER` — we don't refuse it, but we don't enforce
///   namespace on the trigger name. The trigger's body could reference
///   non-namespaced tables; that's load-bearing because a trigger may
///   legitimately read from launcher-owned tables. v1 accepts this
///   gap; v2 may add trigger-body validation.
/// - `CREATE VIEW` — same posture as triggers.
/// - `DROP TABLE` — refuse always, since dropping touches table-name
///   surfaces that we'd need to validate against the registry. Module
///   migrations are forward-only; DROPs land via "ship a new migration
///   that does ALTER TABLE ... RENAME" or via launcher-side cleanups.
pub fn validate_sql_namespace(sql: &str, namespace: &str) -> Result<(), String> {
    use regex::Regex;

    let prefix = format!("{}_", namespace);
    let prefix_lc = prefix.to_ascii_lowercase();

    // CREATE [TEMP|TEMPORARY] TABLE [IF NOT EXISTS] "name" | name
    let create_re = Regex::new(
        r#"(?xi)
        CREATE \s+
        (?: (?: TEMP | TEMPORARY ) \s+ )?
        TABLE \s+
        (?: IF \s+ NOT \s+ EXISTS \s+ )?
        (?: " (?P<qname> [^"]+ ) " | (?P<name> [A-Za-z_][A-Za-z0-9_]* ) )
        "#,
    )
    .map_err(|e| format!("compile CREATE TABLE regex: {}", e))?;

    for cap in create_re.captures_iter(sql) {
        let name = cap
            .name("qname")
            .or_else(|| cap.name("name"))
            .map(|m| m.as_str().to_string())
            .unwrap_or_default();
        if name.is_empty() {
            continue;
        }
        if !name.to_ascii_lowercase().starts_with(&prefix_lc) {
            return Err(format!(
                "CREATE TABLE '{}' is outside the module's namespace; \
                 all module-owned tables must start with '{}'",
                name, prefix
            ));
        }
    }

    // ALTER TABLE "name" | name
    let alter_re = Regex::new(
        r#"(?xi)
        ALTER \s+ TABLE \s+
        (?: " (?P<qname> [^"]+ ) " | (?P<name> [A-Za-z_][A-Za-z0-9_]* ) )
        "#,
    )
    .map_err(|e| format!("compile ALTER TABLE regex: {}", e))?;

    for cap in alter_re.captures_iter(sql) {
        let name = cap
            .name("qname")
            .or_else(|| cap.name("name"))
            .map(|m| m.as_str().to_string())
            .unwrap_or_default();
        if name.is_empty() {
            continue;
        }
        if !name.to_ascii_lowercase().starts_with(&prefix_lc) {
            return Err(format!(
                "ALTER TABLE '{}' is outside the module's namespace; \
                 modules may only ALTER their own tables (prefix '{}')",
                name, prefix
            ));
        }
    }

    // CREATE [UNIQUE] INDEX [IF NOT EXISTS] idx ON "table" | table
    // Validates the TARGET table — index name itself doesn't have to
    // match the namespace (though convention is to prefix it too).
    let index_re = Regex::new(
        r#"(?xi)
        CREATE \s+
        (?: UNIQUE \s+ )?
        INDEX \s+
        (?: IF \s+ NOT \s+ EXISTS \s+ )?
        (?: " [^"]+ " | [A-Za-z_][A-Za-z0-9_]* )
        \s+ ON \s+
        (?: " (?P<qtbl> [^"]+ ) " | (?P<tbl> [A-Za-z_][A-Za-z0-9_]* ) )
        "#,
    )
    .map_err(|e| format!("compile CREATE INDEX regex: {}", e))?;

    for cap in index_re.captures_iter(sql) {
        let tbl = cap
            .name("qtbl")
            .or_else(|| cap.name("tbl"))
            .map(|m| m.as_str().to_string())
            .unwrap_or_default();
        if tbl.is_empty() {
            continue;
        }
        if !tbl.to_ascii_lowercase().starts_with(&prefix_lc) {
            return Err(format!(
                "CREATE INDEX on table '{}' is outside the module's namespace; \
                 modules may only index their own tables (prefix '{}')",
                tbl, prefix
            ));
        }
    }

    // DROP TABLE — refuse always. Module migrations are forward-only;
    // dropping a launcher-owned table would be catastrophic, dropping
    // a module-owned table can be expressed as a sequence of ALTER /
    // CREATE in a new migration file.
    let drop_re = Regex::new(r"(?i)\bDROP\s+TABLE\b")
        .map_err(|e| format!("compile DROP TABLE regex: {}", e))?;
    if drop_re.is_match(sql) {
        return Err(
            "DROP TABLE is not allowed in module migrations; \
             module migrations are forward-only. To remove a column or rename a table, \
             ship a new migration with CREATE/INSERT/ALTER steps."
                .into(),
        );
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn open_test_db() -> Db {
        Db::open_in_memory().expect("open in-memory db with all migrations applied")
    }

    fn write_migration_files(dir: &Path, files: &[(&str, &str)]) {
        for (name, content) in files {
            std::fs::write(dir.join(name), content).expect("write migration file");
        }
    }

    fn make_manifest(namespace: &str) -> ModuleManifest {
        // Minimal valid manifest — just enough that `db` is wired.
        let json = format!(
            r#"{{
                "manifest_version": 1,
                "id": "test-mod",
                "name": "Test",
                "version": "0.0.1",
                "category": "community",
                "license": {{ "min_orchestrator_tier": "free" }},
                "install": {{
                    "method": "local",
                    "install_dir": "{{VCT_MODULES}}/test"
                }},
                "runtime": {{
                    "type": "service",
                    "command": "echo"
                }},
                "db": {{
                    "migrations_dir": "db/",
                    "namespace": "{}"
                }}
            }}"#,
            namespace,
        );
        ModuleManifest::from_json(&json).expect("parse manifest")
    }

    // ─── Happy path ─────────────────────────────────────────────────────

    #[test]
    fn applies_two_files_then_skips_on_re_apply() {
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        let migrations_dir = tmp.path().join("db");
        std::fs::create_dir(&migrations_dir).unwrap();

        write_migration_files(
            &migrations_dir,
            &[
                ("0001_state.sql", "CREATE TABLE rl_state (id INTEGER PRIMARY KEY);"),
                ("0002_runs.sql", "CREATE TABLE rl_runs (id INTEGER PRIMARY KEY);"),
            ],
        );
        let manifest = make_manifest("rl");

        let report = apply_module_db_migrations(&db, "test-mod", tmp.path(), &manifest)
            .expect("apply ok");
        assert!(report.ok(), "first apply errors: {:?}", report.errors);
        assert_eq!(report.applied, vec!["0001_state.sql", "0002_runs.sql"]);
        assert!(report.skipped.is_empty());

        // Tables exist.
        let count: i64 = db
            .lock()
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('rl_state','rl_runs')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 2);

        // Second run skips both (sha matches, no error).
        let report2 = apply_module_db_migrations(&db, "test-mod", tmp.path(), &manifest)
            .expect("apply ok 2");
        assert!(report2.ok());
        assert!(report2.applied.is_empty());
        assert_eq!(report2.skipped, vec!["0001_state.sql", "0002_runs.sql"]);
    }

    #[test]
    fn missing_db_block_returns_empty_report() {
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        // Manifest with NO db block.
        let m = ModuleManifest::from_json(
            r#"{
                "manifest_version": 1,
                "id": "no-db-mod",
                "name": "No DB",
                "version": "0.0.1",
                "category": "community",
                "license": { "min_orchestrator_tier": "free" },
                "install": { "method": "local", "install_dir": "{VCT_MODULES}/no" },
                "runtime": { "type": "cli", "command": "echo" }
            }"#,
        )
        .unwrap();

        let report =
            apply_module_db_migrations(&db, "no-db-mod", tmp.path(), &m).expect("apply ok");
        assert!(report.ok());
        assert!(report.applied.is_empty());
        assert!(report.skipped.is_empty());
    }

    #[test]
    fn missing_migrations_dir_returns_empty_report() {
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        // Don't create the db/ subdir.
        let manifest = make_manifest("rl");

        let report = apply_module_db_migrations(&db, "test-mod", tmp.path(), &manifest)
            .expect("apply ok");
        assert!(report.ok());
        assert!(report.applied.is_empty());
    }

    // ─── Sha mismatch ───────────────────────────────────────────────────

    #[test]
    fn sha_mismatch_refused_on_second_apply() {
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        let migrations_dir = tmp.path().join("db");
        std::fs::create_dir(&migrations_dir).unwrap();

        write_migration_files(
            &migrations_dir,
            &[("0001_state.sql", "CREATE TABLE rl_state (id INTEGER PRIMARY KEY);")],
        );
        let manifest = make_manifest("rl");

        apply_module_db_migrations(&db, "test-mod", tmp.path(), &manifest)
            .expect("first apply")
            .ok()
            .then(|| ())
            .expect("first apply ok");

        // Mutate the file (illegal — modules MUST ship new files instead).
        std::fs::write(
            migrations_dir.join("0001_state.sql"),
            "CREATE TABLE rl_state (id INTEGER PRIMARY KEY, name TEXT);",
        )
        .unwrap();

        let report = apply_module_db_migrations(&db, "test-mod", tmp.path(), &manifest)
            .expect("apply returns Ok with errors-in-report");
        assert!(!report.ok());
        assert_eq!(report.errors.len(), 1);
        assert_eq!(report.errors[0].kind, MigrationErrorKind::ShaMismatch);
        assert_eq!(report.errors[0].filename, "0001_state.sql");
    }

    // ─── Namespace violation ────────────────────────────────────────────

    #[test]
    fn namespace_violation_refuses_create_outside_prefix() {
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        let migrations_dir = tmp.path().join("db");
        std::fs::create_dir(&migrations_dir).unwrap();

        write_migration_files(
            &migrations_dir,
            &[("0001_bad.sql", "CREATE TABLE not_rl_state (id INTEGER);")],
        );
        let manifest = make_manifest("rl");

        let report = apply_module_db_migrations(&db, "test-mod", tmp.path(), &manifest)
            .expect("apply returns Ok with errors-in-report");
        assert!(!report.ok());
        assert_eq!(report.errors[0].kind, MigrationErrorKind::NamespaceViolation);

        // Table was NOT created (validation happens before execution).
        let count: i64 = db
            .lock()
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='not_rl_state'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn namespace_validation_accepts_fk_to_launcher_tables() {
        // CREATE TABLE in namespace, but with FK to projects(id) —
        // that's allowed (FK target is launcher-owned).
        let result = validate_sql_namespace(
            "CREATE TABLE rl_state (project_id TEXT, FOREIGN KEY (project_id) REFERENCES projects(id));",
            "rl",
        );
        assert!(result.is_ok(), "FK to projects(id) must be allowed: {:?}", result);
    }

    #[test]
    fn namespace_validation_rejects_alter_outside_namespace() {
        let result =
            validate_sql_namespace("ALTER TABLE projects ADD COLUMN foo TEXT;", "rl");
        assert!(result.is_err(), "ALTER on projects must be refused");
        assert!(result.unwrap_err().contains("ALTER TABLE"));
    }

    #[test]
    fn namespace_validation_accepts_alter_inside_namespace() {
        let result = validate_sql_namespace(
            "ALTER TABLE rl_state ADD COLUMN extra TEXT;",
            "rl",
        );
        assert!(result.is_ok());
    }

    #[test]
    fn namespace_validation_rejects_index_outside_namespace() {
        let result = validate_sql_namespace(
            "CREATE INDEX idx_x ON other_table(foo);",
            "rl",
        );
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("CREATE INDEX"));
    }

    #[test]
    fn namespace_validation_accepts_index_inside_namespace() {
        let result = validate_sql_namespace(
            "CREATE INDEX idx_rl_state_proj ON rl_state(project_id);",
            "rl",
        );
        assert!(result.is_ok());
    }

    #[test]
    fn namespace_validation_handles_case_insensitive_keywords() {
        // Lowercase keywords should still be recognized.
        let result = validate_sql_namespace(
            "create table rl_state (id integer);",
            "rl",
        );
        assert!(result.is_ok());

        let result_bad = validate_sql_namespace(
            "create table other (id integer);",
            "rl",
        );
        assert!(result_bad.is_err());
    }

    #[test]
    fn namespace_validation_refuses_drop_table() {
        let result = validate_sql_namespace("DROP TABLE rl_state;", "rl");
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("DROP TABLE"));
    }

    #[test]
    fn namespace_validation_handles_if_not_exists() {
        let result = validate_sql_namespace(
            "CREATE TABLE IF NOT EXISTS rl_state (id INTEGER);",
            "rl",
        );
        assert!(result.is_ok());

        let bad = validate_sql_namespace(
            "CREATE TABLE IF NOT EXISTS other_table (id INTEGER);",
            "rl",
        );
        assert!(bad.is_err());
    }

    // ─── Namespace collision (supplemental requirement) ─────────────────

    #[test]
    fn namespace_collision_refused_across_modules() {
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        let migrations_dir = tmp.path().join("db");
        std::fs::create_dir(&migrations_dir).unwrap();
        write_migration_files(
            &migrations_dir,
            &[("0001_state.sql", "CREATE TABLE rl_state (id INTEGER PRIMARY KEY);")],
        );
        let manifest = make_manifest("rl");

        // First module: applies successfully.
        let r1 = apply_module_db_migrations(&db, "module-a", tmp.path(), &manifest)
            .expect("module-a apply");
        assert!(r1.ok(), "first apply errors: {:?}", r1.errors);

        // Second module with SAME namespace: refused.
        let tmp2 = tempfile::tempdir().unwrap();
        let mig_dir2 = tmp2.path().join("db");
        std::fs::create_dir(&mig_dir2).unwrap();
        write_migration_files(
            &mig_dir2,
            &[("0001_other.sql", "CREATE TABLE rl_other (id INTEGER);")],
        );
        let manifest2 = make_manifest("rl");

        let r2 = apply_module_db_migrations(&db, "module-b", tmp2.path(), &manifest2)
            .expect("module-b apply returns report");
        assert!(!r2.ok(), "second module should fail with collision");
        assert_eq!(r2.errors.len(), 1);
        assert_eq!(r2.errors[0].kind, MigrationErrorKind::NamespaceCollision);
        assert!(
            r2.errors[0].message.contains("module-a"),
            "collision message should name conflicting module, got: {}",
            r2.errors[0].message
        );
    }

    #[test]
    fn namespace_collision_does_not_block_same_module_reapply() {
        // module-a re-applies its own migrations: SAME namespace, but
        // SAME module_id → not a collision.
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        let migrations_dir = tmp.path().join("db");
        std::fs::create_dir(&migrations_dir).unwrap();
        write_migration_files(
            &migrations_dir,
            &[("0001_state.sql", "CREATE TABLE rl_state (id INTEGER PRIMARY KEY);")],
        );
        let manifest = make_manifest("rl");

        apply_module_db_migrations(&db, "module-a", tmp.path(), &manifest).unwrap();
        let r2 = apply_module_db_migrations(&db, "module-a", tmp.path(), &manifest).unwrap();
        assert!(r2.ok(), "self re-apply must not collide with itself");
        assert_eq!(r2.skipped, vec!["0001_state.sql"]);
    }

    // ─── File discovery ─────────────────────────────────────────────────

    #[test]
    fn ignores_files_without_numeric_prefix() {
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        let migrations_dir = tmp.path().join("db");
        std::fs::create_dir(&migrations_dir).unwrap();

        write_migration_files(
            &migrations_dir,
            &[
                ("README.md", "ignore me"),
                ("setup.sql", "CREATE TABLE rl_other (id INTEGER);"),
                ("0001_real.sql", "CREATE TABLE rl_state (id INTEGER);"),
            ],
        );
        let manifest = make_manifest("rl");

        let report = apply_module_db_migrations(&db, "test-mod", tmp.path(), &manifest)
            .expect("apply ok");
        assert!(report.ok());
        // Only 0001_real.sql qualifies.
        assert_eq!(report.applied, vec!["0001_real.sql"]);
    }

    #[test]
    fn applies_files_in_numeric_order() {
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        let migrations_dir = tmp.path().join("db");
        std::fs::create_dir(&migrations_dir).unwrap();

        // Write out-of-order; lexicographic sort happens to match
        // numeric for zero-padded names.
        write_migration_files(
            &migrations_dir,
            &[
                ("0003_third.sql", "CREATE TABLE rl_three (id INTEGER);"),
                ("0001_first.sql", "CREATE TABLE rl_one (id INTEGER);"),
                ("0002_second.sql", "CREATE TABLE rl_two (id INTEGER);"),
            ],
        );
        let manifest = make_manifest("rl");

        let report = apply_module_db_migrations(&db, "test-mod", tmp.path(), &manifest)
            .expect("apply ok");
        assert!(report.ok());
        assert_eq!(
            report.applied,
            vec!["0001_first.sql", "0002_second.sql", "0003_third.sql"],
        );
    }

    // ─── SQL execution errors ───────────────────────────────────────────

    #[test]
    fn syntax_error_surfaces_with_sql_execution_kind() {
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        let migrations_dir = tmp.path().join("db");
        std::fs::create_dir(&migrations_dir).unwrap();

        // Looks like CREATE TABLE rl_* (passes namespace), but SQL syntax invalid.
        // SQLite is lenient with column-type names; use a real syntax error
        // (unclosed parenthesis) so the parser actually rejects it.
        write_migration_files(
            &migrations_dir,
            &[("0001_bad.sql", "CREATE TABLE rl_bad (id INTEGER PRIMARY KEY")],
        );
        let manifest = make_manifest("rl");

        let report = apply_module_db_migrations(&db, "test-mod", tmp.path(), &manifest)
            .expect("apply returns Ok with error-in-report");
        assert!(!report.ok());
        assert_eq!(report.errors[0].kind, MigrationErrorKind::SqlExecutionFailed);
    }

    #[test]
    fn first_error_stops_apply_pass() {
        let db = open_test_db();
        let tmp = tempfile::tempdir().unwrap();
        let migrations_dir = tmp.path().join("db");
        std::fs::create_dir(&migrations_dir).unwrap();

        write_migration_files(
            &migrations_dir,
            &[
                ("0001_first.sql", "CREATE TABLE rl_one (id INTEGER);"),
                ("0002_bad.sql", "CREATE TABLE other_namespace_table (id INTEGER);"),
                ("0003_third.sql", "CREATE TABLE rl_three (id INTEGER);"),
            ],
        );
        let manifest = make_manifest("rl");

        let report = apply_module_db_migrations(&db, "test-mod", tmp.path(), &manifest)
            .expect("apply ok");
        assert!(!report.ok());
        assert_eq!(report.applied, vec!["0001_first.sql"]);
        assert_eq!(report.errors[0].filename, "0002_bad.sql");

        // rl_three was NOT applied even though it's valid.
        let count: i64 = db
            .lock()
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='rl_three'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
    }

    // ─── File discovery shape ───────────────────────────────────────────

    #[test]
    fn is_migration_file_recognizes_canonical_shape() {
        use std::path::PathBuf;
        assert!(is_migration_file(&PathBuf::from("0001_init.sql")));
        assert!(is_migration_file(&PathBuf::from("42_add_idx.sql")));
        assert!(is_migration_file(&PathBuf::from("1_x.sql")));
    }

    #[test]
    fn is_migration_file_rejects_other_shapes() {
        use std::path::PathBuf;
        assert!(!is_migration_file(&PathBuf::from("README.md")));
        assert!(!is_migration_file(&PathBuf::from("_skip.sql")));
        assert!(!is_migration_file(&PathBuf::from("setup.sql")));
        assert!(!is_migration_file(&PathBuf::from("001.sql"))); // no underscore
        assert!(!is_migration_file(&PathBuf::from("0001_x.txt")));
    }
}
