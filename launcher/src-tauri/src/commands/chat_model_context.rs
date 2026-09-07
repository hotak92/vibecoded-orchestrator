// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Tauri command surface for the chat-model context table (v0.2.92, WP-11).
//!
//! `launcher.db` is canonical (`vct_launcher_core::db::chat_model_context`,
//! migration 043); this layer owns the two things the DB layer deliberately
//! does not: FILE I/O and PATHS.
//!
//!   * **Seed** — read the ONE shipped seed file,
//!     `<orchestrator_root>/claude_mcp_servers/model_router/chat_model_context.seed.json`,
//!     the same file the gateway carries inside its own wheel as a fallback.
//!     Read, never copied: an `include_str!` reaching out of the crate — or a
//!     seed inlined into the migration SQL — would fork the shipped data into
//!     two copies with two update paths.
//!   * **Export** — write `<vct_root>/model-gateway/chat_model_context.json`,
//!     which the gateway reads. On EVERY mutation and on EVERY boot, because
//!     a table edit that never reaches the file is a preference the daemon
//!     never sees.
//!
//! ## Why the export exists at all, when a hub route also serves the table
//!
//! The gateway is deliberately hub-independent: it must keep advertising the
//! right context windows when neither the launcher nor `vct-hub` is running.
//! A file it can `stat` costs it nothing and has no liveness requirement. The
//! hub route (`GET /api/v1/chat-model-context`) exists for CLIs and agents,
//! not for the gateway.
//!
//! ## The failure that must never be silent
//!
//! If the export write fails, the DB write has already committed — the user
//! sees their edit in the pane while the gateway keeps serving the old file.
//! So every mutating command returns the export outcome alongside its own
//! result and the pane renders the failure. The alternative (log-and-forget)
//! is the shipped-a-preference-nothing-consumes defect with a log line
//! attached.

use std::path::{Path, PathBuf};

use serde::Serialize;
use tauri::{command, State};

use vct_launcher_core::db::chat_model_context::{
    export_document, now_iso8601_utc, parse_document, ChatModelContextInput, ChatModelContextRow,
    ReseedOutcome, EXPORT_SCHEMA_VERSION,
};
use vct_launcher_core::paths::vct_root_dir;

use crate::db::Db;
use crate::json_file::{atomic_write_json, BackupPolicy};

// ─── Paths ────────────────────────────────────────────────────────────────

/// Subdirectory of `<vct_root>` holding the gateway's state files.
///
/// MUST MATCH `claude_mcp_servers/model_router/config.py` (`_STATE_SUBDIR`,
/// `_EXPORT_BASENAME`, and the `VCT_MODEL_GATEWAY_CONTEXT_TABLE` env name).
/// The two are held in lockstep by
/// `tests/test_v0292_chat_model_context_contract.py`, which reads both source
/// files and compares the literals — this is a deliberate (C)-tier mirror
/// under the repo's A>B>C rule: (A) shelling to Python to resolve a two-
/// segment path would make the launcher's boot seed depend on the gateway
/// package being importable, which is exactly backwards (the launcher writes
/// the file the gateway may never be installed to read).
const GATEWAY_STATE_SUBDIR: &str = "model-gateway";
const EXPORT_BASENAME: &str = "chat_model_context.json";
const EXPORT_PATH_ENV: &str = "VCT_MODEL_GATEWAY_CONTEXT_TABLE";

/// Where the shipped seed lives inside the orchestrator clone.
const SEED_RELATIVE_PATH: [&str; 3] = [
    "claude_mcp_servers",
    "model_router",
    "chat_model_context.seed.json",
];

/// Where the launcher WRITES the table for the gateway to read.
///
/// Honours `VCT_MODEL_GATEWAY_CONTEXT_TABLE` for the same reason the gateway
/// does: a user who redirects the table redirects BOTH ends of it, otherwise
/// the launcher would keep refreshing a file nothing reads while the gateway
/// waited on a file nothing writes. The two processes resolve it
/// independently from their own environments, so a mismatch is possible —
/// which is why `chat_model_context_status` reports the resolved path and
/// whether an override was in effect, making the mismatch visible rather
/// than mysterious.
pub fn export_path() -> PathBuf {
    if let Ok(custom) = std::env::var(EXPORT_PATH_ENV) {
        let trimmed = custom.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    vct_root_dir().join(GATEWAY_STATE_SUBDIR).join(EXPORT_BASENAME)
}

/// Absolute path of the shipped seed, or `None` when no orchestrator clone is
/// discoverable.
///
/// Resolution goes through `installer::resolve_orchestrator_root(db)` — the
/// canonical resolver, which reads `app_state['launcher.install_path']`
/// (seeded by `install.py`, which is why this works when the launcher binary
/// lives OUTSIDE the clone) and falls back to a `current_exe()` walk-up. No
/// `env!("CARGO_MANIFEST_DIR")`, no assumed layout: both would bake the build
/// host's path into a shipped binary.
pub fn seed_path(db: &Db) -> Option<PathBuf> {
    let root = crate::commands::installer::resolve_orchestrator_root(db)?;
    let mut p = root;
    for seg in SEED_RELATIVE_PATH {
        p = p.join(seg);
    }
    Some(p)
}

// ─── Seed loading ─────────────────────────────────────────────────────────

/// Read + parse the shipped seed. `Ok(None)` = "no clone / no file", which is
/// a normal state on a binary-only install and NOT an error: the gateway
/// ships the same seed inside its own wheel and serves it when no export
/// exists. `Err` = the file is there and unusable, which is a damaged install
/// and says so.
pub fn load_seed_rows(db: &Db) -> Result<Option<(PathBuf, Vec<ChatModelContextInput>)>, String> {
    let Some(path) = seed_path(db) else {
        return Ok(None);
    };
    if !path.exists() {
        return Ok(None);
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| format!("read shipped seed {}: {}", path.display(), e))?;
    let value: serde_json::Value = serde_json::from_str(&raw)
        .map_err(|e| format!("parse shipped seed {}: {}", path.display(), e))?;
    let rows = parse_document(&value)
        .map_err(|e| format!("shipped seed {} is not usable: {}", path.display(), e))?;
    Ok(Some((path, rows)))
}

// ─── Export ───────────────────────────────────────────────────────────────

/// What an export attempt did. `ok == false` is rendered by the pane: the
/// table and the file have diverged and the gateway is serving stale data.
#[derive(Debug, Clone, Serialize)]
pub struct ExportReport {
    pub ok: bool,
    /// Resolved absolute path, always reported (even on failure) so the
    /// user can look at the right file.
    pub path: String,
    /// `true` when `VCT_MODEL_GATEWAY_CONTEXT_TABLE` redirected the path.
    pub path_overridden_by_env: bool,
    pub models: usize,
    /// ISO-8601 UTC stamp written into the document, on success.
    pub generated_at: Option<String>,
    pub error: Option<String>,
}

/// Write the table to the gateway-readable file. Callers treat a failure as
/// reportable, never fatal: the DB is the source of truth and a later export
/// (next mutation, next boot) converges.
///
/// Goes through `json_file::atomic_write_json` (lock + temp + rename) rather
/// than hand-formatting, so the key order the launcher's `preserve_order`
/// `serde_json` produced is exactly what lands on disk, and a crash mid-write
/// cannot leave the gateway a truncated file.
///
/// Backup policy `Once`: on a normal machine the first export finds no file
/// and writes NO sidecar (`write_backup` returns early when the target does
/// not exist), so the directory stays clean. A sidecar appears only if
/// something was already at that path before VCO first wrote it — the
/// already-damaged case, and the one time a copy is worth keeping.
pub fn export_now(db: &Db) -> ExportReport {
    let path = export_path();
    let overridden = std::env::var(EXPORT_PATH_ENV)
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false);

    let rows = match db.list_chat_model_context() {
        Ok(r) => r,
        Err(e) => {
            return ExportReport {
                ok: false,
                path: path.display().to_string(),
                path_overridden_by_env: overridden,
                models: 0,
                generated_at: None,
                error: Some(format!("could not read the table: {e}")),
            }
        }
    };
    let generated_at = now_iso8601_utc();
    let doc = export_document(&rows, &generated_at);

    match atomic_write_json(&path, &doc, BackupPolicy::Once { ext: "pre-vco" }) {
        Ok(()) => ExportReport {
            ok: true,
            path: path.display().to_string(),
            path_overridden_by_env: overridden,
            models: rows.len(),
            generated_at: Some(generated_at),
            error: None,
        },
        Err(e) => ExportReport {
            ok: false,
            path: path.display().to_string(),
            path_overridden_by_env: overridden,
            models: rows.len(),
            generated_at: None,
            error: Some(format!(
                "could not write {}: {e}. The table is saved, but the model \
                 gateway will keep serving the previous file until this \
                 succeeds.",
                path.display()
            )),
        },
    }
}

// ─── Boot ─────────────────────────────────────────────────────────────────

/// First-boot seed + export, called once from `lib.rs::run`.
///
/// Soft-fail end to end: nothing here may block the launcher from starting.
/// Every failure path logs a line a user can act on, and none of them leave
/// the table half-written (the seeder validates the whole batch first, in one
/// transaction).
pub fn seed_and_export_on_boot(db: &Db) {
    match load_seed_rows(db) {
        Ok(Some((path, rows))) => match db.seed_chat_model_context_if_empty(&rows) {
            Ok(0) => {}
            Ok(n) => tracing::info!(
                "[vct] chat-model context: seeded {} row(s) from {}",
                n,
                path.display()
            ),
            Err(e) => tracing::warn!("[vct] chat-model context: seeding failed: {}", e),
        },
        Ok(None) => {
            // Not a degraded state: the gateway ships the same seed inside
            // its own wheel and serves it whenever no export exists.
            tracing::debug!(
                "[vct] chat-model context: no shipped seed found (no orchestrator \
                 clone resolved); the model gateway falls back to its bundled copy"
            );
        }
        Err(e) => tracing::warn!(
            "[vct] chat-model context: {} — re-run `python install.py` if this persists",
            e
        ),
    }

    let report = export_now(db);
    if report.ok {
        tracing::info!(
            "[vct] chat-model context: exported {} model(s) to {}",
            report.models,
            report.path
        );
    } else {
        tracing::warn!(
            "[vct] chat-model context: export to {} failed: {}",
            report.path,
            report.error.as_deref().unwrap_or("unknown error")
        );
    }
}

// ─── Status ───────────────────────────────────────────────────────────────

/// What the pane shows above the table. Every field is read by the pane; none
/// is decorative.
#[derive(Debug, Clone, Serialize)]
pub struct ChatModelContextStatus {
    pub rows: usize,
    pub user_edited_rows: usize,
    pub export_path: String,
    pub export_path_overridden_by_env: bool,
    pub export_exists: bool,
    /// `generated_at` read back OUT of the file on disk — the honest answer
    /// to "what does the gateway currently see", which is not the same
    /// question as "when did we last try to write".
    pub export_generated_at: Option<String>,
    pub export_models: Option<usize>,
    /// Set when the file exists but the gateway will refuse it: unparseable,
    /// or a schema version this launcher did not write. Surfaced so the
    /// already-damaged case is visible in the GUI instead of only in the
    /// gateway's log.
    pub export_problem: Option<String>,
    pub seed_path: Option<String>,
    pub seed_available: bool,
    pub seed_problem: Option<String>,
}

fn inspect_export(path: &Path) -> (bool, Option<String>, Option<usize>, Option<String>) {
    if !path.exists() {
        return (false, None, None, None);
    }
    let raw = match std::fs::read_to_string(path) {
        Ok(r) => r,
        Err(e) => return (true, None, None, Some(format!("could not read it: {e}"))),
    };
    let value: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            return (
                true,
                None,
                None,
                Some(format!(
                    "it is not valid JSON ({e}); the gateway is ignoring it and \
                     serving its own bundled table. The next export overwrites it."
                )),
            )
        }
    };
    let generated_at = value
        .get("generated_at")
        .and_then(|v| v.as_str())
        .map(str::to_string);
    let version = value.get("schema_version").and_then(|v| v.as_u64());
    let models = value
        .get("models")
        .and_then(|v| v.as_object())
        .map(|m| m.len());
    let problem = match version {
        Some(v) if v == EXPORT_SCHEMA_VERSION => None,
        Some(v) => Some(format!(
            "it declares schema_version {v}, but this launcher writes \
             {EXPORT_SCHEMA_VERSION}; the gateway is ignoring it and serving its \
             own bundled table. The next export overwrites it."
        )),
        None => Some(
            "it has no schema_version; the gateway is ignoring it and serving its \
             own bundled table. The next export overwrites it."
                .to_string(),
        ),
    };
    (true, generated_at, models, problem)
}

// ─── Commands ─────────────────────────────────────────────────────────────

/// Result of a mutating command: what changed, and whether the gateway got
/// it. Both halves always travel together — see the module doc.
#[derive(Debug, Clone, Serialize)]
pub struct ChatModelContextMutation {
    pub row: Option<ChatModelContextRow>,
    pub deleted: bool,
    pub reseed: Option<ReseedOutcome>,
    pub export: ExportReport,
}

#[command]
pub async fn chat_model_context_list(
    db: State<'_, Db>,
) -> Result<Vec<ChatModelContextRow>, String> {
    db.list_chat_model_context()
}

#[command]
pub async fn chat_model_context_status(
    db: State<'_, Db>,
) -> Result<ChatModelContextStatus, String> {
    let rows = db.list_chat_model_context()?;
    let path = export_path();
    let (exists, generated_at, models, problem) = inspect_export(&path);

    let (seed_path_str, seed_available, seed_problem) = match load_seed_rows(&db) {
        Ok(Some((p, _))) => (Some(p.display().to_string()), true, None),
        Ok(None) => (
            seed_path(&db).map(|p| p.display().to_string()),
            false,
            None,
        ),
        Err(e) => (seed_path(&db).map(|p| p.display().to_string()), false, Some(e)),
    };

    Ok(ChatModelContextStatus {
        rows: rows.len(),
        user_edited_rows: rows.iter().filter(|r| r.user_edited).count(),
        export_path: path.display().to_string(),
        export_path_overridden_by_env: std::env::var(EXPORT_PATH_ENV)
            .map(|v| !v.trim().is_empty())
            .unwrap_or(false),
        export_exists: exists,
        export_generated_at: generated_at,
        export_models: models,
        export_problem: problem,
        seed_path: seed_path_str,
        seed_available,
        seed_problem,
    })
}

// The three mutators live as PLAIN FUNCTIONS taking `&Db`, with the `#[command]`
// wrappers below reduced to argument marshalling. That is what makes
// "every mutation re-exports" testable: a `#[command] async fn` takes
// `State<'_, Db>`, which a unit test cannot construct without a Tauri app, so
// keeping the logic in the command body would leave the export-on-mutation
// promise backed by nothing but a code reading.

/// Insert or update one row FROM THE GUI, so `user_edited = 1` — which is
/// what protects it from the next reseed — then re-export.
pub fn upsert_and_export(
    db: &Db,
    input: ChatModelContextInput,
) -> Result<ChatModelContextMutation, String> {
    let row = db.upsert_chat_model_context(input, true)?;
    Ok(ChatModelContextMutation {
        row: Some(row),
        deleted: false,
        reseed: None,
        export: export_now(db),
    })
}

/// Delete one row, then re-export. A missing row is `deleted: false`, not an
/// error — but it still re-exports, so a stale file left by an earlier failed
/// export converges on the next attempt.
pub fn delete_and_export(db: &Db, model_id: &str) -> Result<ChatModelContextMutation, String> {
    let deleted = db.delete_chat_model_context(model_id.trim())?;
    Ok(ChatModelContextMutation {
        row: None,
        deleted,
        reseed: None,
        export: export_now(db),
    })
}

/// Re-apply the shipped rows, never over a user edit, then re-export.
///
/// Fails LOUDLY when the shipped seed cannot be found: the button's label
/// promises "from shipped defaults", and quietly doing nothing would be that
/// promise going unbacked.
pub fn reseed_and_export(db: &Db) -> Result<ChatModelContextMutation, String> {
    let rows = match load_seed_rows(db)? {
        Some((_, rows)) => rows,
        None => {
            return Err(
                "the shipped defaults file could not be located. It lives at \
                 claude_mcp_servers/model_router/chat_model_context.seed.json inside \
                 the orchestrator clone; this launcher could not resolve a clone, so \
                 there is nothing to reseed from. The model gateway still has its own \
                 bundled copy — this only affects editing the table here."
                    .to_string(),
            )
        }
    };
    let outcome = db.reseed_chat_model_context(&rows)?;
    Ok(ChatModelContextMutation {
        row: None,
        deleted: false,
        reseed: Some(outcome),
        export: export_now(db),
    })
}

#[command]
pub async fn chat_model_context_upsert(
    input: ChatModelContextInput,
    db: State<'_, Db>,
) -> Result<ChatModelContextMutation, String> {
    upsert_and_export(&db, input)
}

#[command]
pub async fn chat_model_context_delete(
    model_id: String,
    db: State<'_, Db>,
) -> Result<ChatModelContextMutation, String> {
    delete_and_export(&db, &model_id)
}

#[command]
pub async fn chat_model_context_reseed(
    db: State<'_, Db>,
) -> Result<ChatModelContextMutation, String> {
    reseed_and_export(&db)
}

/// Force an export without changing anything — the pane's "Export now".
#[command]
pub async fn chat_model_context_export(db: State<'_, Db>) -> Result<ExportReport, String> {
    Ok(export_now(&db))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// In-memory launcher.db. NEVER opens the real `~/.vct/launcher.db`, and
    /// nothing here reaches hub-spawning code.
    fn make_db() -> Db {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        vct_launcher_core::db::migrations::apply(&conn).unwrap();
        Db(Mutex::new(conn))
    }

    fn input(model_id: &str) -> ChatModelContextInput {
        ChatModelContextInput {
            model_id: model_id.into(),
            vendor: "zai".into(),
            context_window: 200_000,
            max_output: 128_000,
            window_1m: false,
            source: "https://docs.z.ai/guides/llm/glm-5.1".into(),
            source_note: String::new(),
        }
    }

    /// A directory `resolve_orchestrator_root` accepts as a clone (install.py
    /// + CLAUDE.md + an `installed` manifest, which is what
    /// `check_install_status` gates the CACHED path on) but which carries NO
    /// shipped seed.
    ///
    /// Pinning this through `app_state` is the only way to make "the seed is
    /// unreachable" deterministic: the resolver's fallback walks up from
    /// `current_exe()`, so a test binary inside this checkout otherwise finds
    /// the real repo root and its real ten-row seed.
    fn clone_without_seed(dir: &Path) -> PathBuf {
        let clone = dir.join("clone-without-seed");
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        clone
    }

    fn tmp_dir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!(
            "vct-cmc-{}-{}",
            tag,
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    // ── path resolution (OS-shape, not OS-specific) ──────────────────────

    /// The default path is `<vct_root>/model-gateway/chat_model_context.json`
    /// on every OS. Asserted as a SHAPE (last two components + parentage of
    /// `vct_root_dir()`), which is what makes it a tri-OS test rather than a
    /// Linux one: the separator and the home-directory convention are the
    /// only per-OS parts and both come from `vct_root_dir`.
    #[test]
    #[serial_test::serial]
    fn default_export_path_is_the_gateway_state_dir_under_vct_root() {
        // Guard: another test in this binary may have set the override.
        std::env::remove_var(EXPORT_PATH_ENV);
        let p = export_path();
        assert_eq!(p.file_name().unwrap(), EXPORT_BASENAME);
        assert_eq!(p.parent().unwrap().file_name().unwrap(), GATEWAY_STATE_SUBDIR);
        assert_eq!(p.parent().unwrap().parent().unwrap(), vct_root_dir());
        assert!(p.is_absolute() || vct_root_dir().is_relative());
    }

    /// The env override the GATEWAY honours is honoured here too, so a user
    /// who redirects the table redirects both ends of it.
    #[test]
    #[serial_test::serial]
    fn export_path_honours_the_gateways_env_override() {
        let dir = tmp_dir("envpath");
        let custom = dir.join("elsewhere.json");
        std::env::set_var(EXPORT_PATH_ENV, &custom);
        assert_eq!(export_path(), custom);

        // Whitespace-only is treated as unset, matching config.py's
        // `.strip()` — otherwise a stray space would silently redirect the
        // export to a relative path.
        std::env::set_var(EXPORT_PATH_ENV, "   ");
        assert_eq!(export_path().file_name().unwrap(), EXPORT_BASENAME);

        std::env::remove_var(EXPORT_PATH_ENV);
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The seed's location inside the clone is composed with `join`, never a
    /// literal `"a/b/c"` string, so it is correct on Windows too.
    #[test]
    fn seed_relative_path_is_composed_not_hardcoded_with_separators() {
        let root = PathBuf::from("ROOT");
        let mut p = root.clone();
        for seg in SEED_RELATIVE_PATH {
            p = p.join(seg);
        }
        assert_eq!(p.file_name().unwrap(), "chat_model_context.seed.json");
        assert_eq!(p.parent().unwrap().file_name().unwrap(), "model_router");
        assert!(
            !SEED_RELATIVE_PATH.iter().any(|s| s.contains('/') || s.contains('\\')),
            "path segments must not embed separators"
        );
    }

    // ── export ───────────────────────────────────────────────────────────

    #[test]
    #[serial_test::serial]
    fn export_writes_the_contract_shape_and_reports_where() {
        let dir = tmp_dir("export");
        let target = dir.join("model-gateway").join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);

        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();

        let report = export_now(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(report.ok, "export failed: {:?}", report.error);
        assert_eq!(report.models, 1);
        assert!(report.path_overridden_by_env);
        assert_eq!(report.path, target.display().to_string());

        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(written["schema_version"], serde_json::json!(1));
        assert_eq!(written["source"], serde_json::json!("launcher.db"));
        assert_eq!(
            written["models"]["glm-5.1"]["context_window"],
            serde_json::json!(200_000)
        );
        // The parent directory is created — the gateway state dir need not
        // pre-exist on a fresh machine.
        assert!(target.parent().unwrap().is_dir());
        std::fs::remove_dir_all(&dir).ok();
    }

    /// ALREADY-DAMAGED: a hand-edited / stale / wrong-schema file at the
    /// export path is a VCO-owned artefact and is overwritten — but the
    /// bytes that were there first are preserved ONCE, so the overwrite is
    /// recoverable.
    #[test]
    #[serial_test::serial]
    fn export_overwrites_a_damaged_file_and_keeps_the_pre_vco_bytes_once() {
        let dir = tmp_dir("damaged");
        let target = dir.join("chat_model_context.json");
        std::fs::write(&target, "{ not json at all").unwrap();
        std::env::set_var(EXPORT_PATH_ENV, &target);

        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();
        let report = export_now(&db);

        // A second export must NOT overwrite the preserved original with a
        // VCO-written copy.
        db.upsert_chat_model_context(input("glm-5.2"), false).unwrap();
        let second = export_now(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(report.ok && second.ok);
        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(written["models"].as_object().unwrap().len(), 2);
        assert_eq!(
            std::fs::read_to_string(dir.join("chat_model_context.json.pre-vco")).unwrap(),
            "{ not json at all",
            "the first-seen bytes are kept once, not replaced on every write"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// A fresh machine gets NO sidecar: `BackupPolicy::Once` only copies a
    /// file that already exists, so the gateway state dir stays clean.
    #[test]
    #[serial_test::serial]
    fn a_first_export_on_a_clean_machine_writes_no_sidecar() {
        let dir = tmp_dir("clean");
        let target = dir.join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);
        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();
        assert!(export_now(&db).ok);
        std::env::remove_var(EXPORT_PATH_ENV);

        let entries: Vec<String> = std::fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        assert_eq!(
            entries,
            vec!["chat_model_context.json"],
            "no backup, no leftover temp or lock file"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// A failing write is REPORTED, not swallowed: the table is saved but
    /// the gateway has not seen it, and the user is told exactly that.
    #[test]
    #[serial_test::serial]
    fn a_failing_export_is_reported_rather_than_logged_and_forgotten() {
        let dir = tmp_dir("failwrite");
        // Point the export at a path whose "parent" is a FILE, so
        // create_dir_all cannot succeed on any OS.
        let blocker = dir.join("not-a-dir");
        std::fs::write(&blocker, "x").unwrap();
        std::env::set_var(EXPORT_PATH_ENV, blocker.join("chat_model_context.json"));

        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();
        let report = export_now(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(!report.ok);
        let msg = report.error.expect("a failure must carry its reason");
        assert!(
            msg.contains("keep serving the previous file"),
            "the message must say what the gateway will do, got: {}",
            msg
        );
        // The row is still in the DB — the export failure did not roll it back.
        assert_eq!(db.list_chat_model_context().unwrap().len(), 1);
        std::fs::remove_dir_all(&dir).ok();
    }

    // ── every mutation re-exports (R16 item 4) ───────────────────────────

    /// A table edit that never reaches the file is a preference the gateway
    /// never sees. Each mutator is proven to re-export, and to REPORT what it
    /// exported — not merely to have an `export_now` call in its body.
    #[test]
    #[serial_test::serial]
    fn every_mutation_re_exports_and_says_so() {
        let dir = tmp_dir("mutations");
        let clone = dir.join("clone");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(
            seed_dir.join("chat_model_context.seed.json"),
            r#"{"schema_version": 1, "models": {
                 "glm-5.2": {"vendor": "zai", "context_window": 1000000,
                             "max_output": 128000, "window_1m": true,
                             "source": "https://docs.z.ai/guides/llm/glm-5.2"}}}"#,
        )
        .unwrap();

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();

        let target = dir.join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);

        let models_on_disk = || -> usize {
            let v: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
            v["models"].as_object().unwrap().len()
        };

        // UPSERT → the row is in the file the gateway reads.
        let m = upsert_and_export(&db, input("glm-5.1")).unwrap();
        assert!(m.export.ok, "{:?}", m.export.error);
        assert_eq!(m.export.models, 1);
        assert_eq!(models_on_disk(), 1);
        assert!(m.row.expect("upsert returns the row").user_edited);

        // RESEED → the shipped row lands in the file too.
        let m = reseed_and_export(&db).unwrap();
        assert!(m.export.ok);
        assert_eq!(m.reseed.expect("reseed reports what it did").inserted, 1);
        assert_eq!(models_on_disk(), 2);

        // DELETE → the removal reaches the file, not just the table.
        let m = delete_and_export(&db, "glm-5.1").unwrap();
        assert!(m.export.ok && m.deleted);
        assert_eq!(models_on_disk(), 1);

        // Deleting something absent is not an error, and still re-exports —
        // which is how a file left stale by an earlier failed export heals.
        let m = delete_and_export(&db, "not-there").unwrap();
        assert!(!m.deleted && m.export.ok);
        assert_eq!(models_on_disk(), 1);

        std::env::remove_var(EXPORT_PATH_ENV);
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The reseed button's label promises "from shipped defaults". With no
    /// defaults reachable it must FAIL LOUDLY rather than report a no-op
    /// success — an unbacked promise is the defect, not the missing file.
    ///
    /// The clone is PINNED via `app_state['launcher.install_path']` rather
    /// than left to discovery. `resolve_orchestrator_root`'s fallback walks
    /// up from `current_exe()`, and a test binary inside this checkout finds
    /// the REAL repo root — and its real seed. A test that relied on "no
    /// clone is discoverable" would assert nothing here while still passing,
    /// which is the test-name-promises-more failure mode.
    #[test]
    #[serial_test::serial]
    fn reseed_with_no_shipped_seed_file_fails_loudly() {
        let dir = tmp_dir("noseed");
        let clone = clone_without_seed(&dir);
        std::env::set_var(EXPORT_PATH_ENV, dir.join("chat_model_context.json"));
        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();
        let err = reseed_and_export(&db).expect_err("must not report success");
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(err.contains("chat_model_context.seed.json"), "got: {}", err);
        assert!(
            err.contains("bundled copy"),
            "the message must say the gateway is still fine, got: {}",
            err
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    // ── status ───────────────────────────────────────────────────────────

    #[test]
    #[serial_test::serial]
    fn status_reports_a_missing_export_without_inventing_a_problem() {
        let dir = tmp_dir("statusmissing");
        std::env::set_var(EXPORT_PATH_ENV, dir.join("chat_model_context.json"));
        let (exists, gen, models, problem) = inspect_export(&export_path());
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(!exists);
        assert_eq!(gen, None);
        assert_eq!(models, None);
        assert_eq!(
            problem, None,
            "an absent export is the normal state on a fresh machine, not a fault"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn status_names_an_unparseable_or_wrong_schema_export() {
        let dir = tmp_dir("statusbad");

        let broken = dir.join("broken.json");
        std::fs::write(&broken, "{{{").unwrap();
        let (exists, _, _, problem) = inspect_export(&broken);
        assert!(exists);
        assert!(problem.unwrap().contains("not valid JSON"));

        let future = dir.join("future.json");
        std::fs::write(
            &future,
            r#"{"schema_version": 99, "generated_at": "t", "models": {}}"#,
        )
        .unwrap();
        let (_, gen, models, problem) = inspect_export(&future);
        assert_eq!(gen.as_deref(), Some("t"));
        assert_eq!(models, Some(0));
        assert!(
            problem.unwrap().contains("schema_version 99"),
            "an unknown schema version is a NAMED condition, distinct from malformed"
        );

        let ok = dir.join("ok.json");
        std::fs::write(
            &ok,
            r#"{"schema_version": 1, "generated_at": "2026-09-02T00:00:00Z", "models": {"a": {}}}"#,
        )
        .unwrap();
        let (_, gen, models, problem) = inspect_export(&ok);
        assert_eq!(gen.as_deref(), Some("2026-09-02T00:00:00Z"));
        assert_eq!(models, Some(1));
        assert_eq!(problem, None, "a good file must not be flagged");

        std::fs::remove_dir_all(&dir).ok();
    }

    // ── the shipped seed, parsed by the code that will parse it ──────────

    /// The REAL shipped seed loads under the launcher's parser, with the ten
    /// cited GLM rows, the four first-party Claude 5 rows (read only by
    /// `vco_lib.vscode_settings.decorate_1m`; the gateway publishes
    /// first-party ids verbatim) and the version-key evidence intact.
    ///
    /// This reads the repo file directly via `CARGO_MANIFEST_DIR`, which is
    /// compile-time-only path resolution INSIDE `#[cfg(test)]` (the same
    /// sanctioned use as `commands::module_gui`'s privacy test). No
    /// production path resolves this way — `seed_path()` goes through
    /// `resolve_orchestrator_root`.
    #[test]
    fn the_shipped_seed_parses_under_the_launchers_own_parser() {
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..");
        let mut seed = repo_root;
        for seg in SEED_RELATIVE_PATH {
            seed = seed.join(seg);
        }
        let raw = std::fs::read_to_string(&seed)
            .unwrap_or_else(|e| panic!("shipped seed {} unreadable: {}", seed.display(), e));
        let value: serde_json::Value = serde_json::from_str(&raw).expect("seed is valid JSON");
        let rows = parse_document(&value).expect("seed parses under the writer's rules");

        assert_eq!(
            rows.len(),
            14,
            "the ten cited GLM rows of handoff §7 plus the four Claude 5 rows"
        );
        assert!(
            rows.iter().all(|r| !r.source.trim().is_empty()),
            "R10: no row without a cited source"
        );

        let by_id = |id: &str| rows.iter().find(|r| r.model_id == id).unwrap_or_else(|| {
            panic!("seed is missing {}", id)
        });
        // The first-party rows: vendor `anthropic`, 1M, cited to Anthropic's
        // docs. They exist for the settings writer's [1m] decoration only.
        for id in ["claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-sonnet-5"] {
            let row = by_id(id);
            assert_eq!(row.vendor, "anthropic", "{}", id);
            assert!(row.window_1m && row.context_window == 1_000_000, "{}", id);
            assert!(row.source.starts_with("https://docs.anthropic.com"), "{}", id);
        }
        // The version-key evidence: same family, 5x apart. If a future edit
        // ever collapses these into a `glm-5*` rule, this reds.
        assert!(by_id("glm-5.2").window_1m && by_id("glm-5.2").context_window == 1_000_000);
        assert!(!by_id("glm-5.1").window_1m && by_id("glm-5.1").context_window == 200_000);
        // The honest citation caveat survives into the rows we will store.
        let air = by_id("glm-4.5-air");
        assert!(
            air.source_note.contains("404") && air.source_note.contains("NOT official"),
            "the glm-4.5-air caveat must reach the DB, or a future editor \
             'corrects' the row backwards from a third-party listing: {:?}",
            air.source_note
        );
        assert!(!air.window_1m, "no unofficial 1M glm-4.5-air");
    }

    /// End-to-end on a synthetic clone: seed → table → export, with the
    /// clone root discovered exactly the way production discovers it
    /// (`app_state['launcher.install_path']`, which `install.py` seeds).
    #[test]
    #[serial_test::serial]
    fn boot_seeds_from_the_clone_and_exports_in_one_pass() {
        let dir = tmp_dir("boot");
        let clone = dir.join("some-odd-place").join("orchestrator");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        // `resolve_orchestrator_root`'s cached-path branch validates the
        // path with `check_install_status` (install.py + CLAUDE.md).
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        // `resolve_orchestrator_root`'s cached-path branch also requires
        // `check_install_status` to pass, which wants an install manifest
        // (or a `.venv/`). Write the manifest — the cheaper of the two.
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(
            seed_dir.join("chat_model_context.seed.json"),
            r#"{"schema_version": 1, "generated_at": "2026-09-02T00:00:00Z",
                "source": "shipped-seed",
                "_comment": "skipped by both parsers",
                "models": {
                  "glm-5.2": {"vendor": "zai", "context_window": 1000000,
                              "max_output": 128000, "window_1m": true,
                              "source": "https://docs.z.ai/guides/llm/glm-5.2"},
                  "glm-5.1": {"vendor": "zai", "context_window": 200000,
                              "max_output": 128000, "window_1m": false,
                              "source": "https://docs.z.ai/guides/llm/glm-5.1"}}}"#,
        )
        .unwrap();

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();

        let target = dir.join("model-gateway").join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);
        seed_and_export_on_boot(&db);

        assert_eq!(db.list_chat_model_context().unwrap().len(), 2);
        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(written["models"]["glm-5.2"]["window_1m"], serde_json::json!(true));
        assert_eq!(written["models"]["glm-5.1"]["window_1m"], serde_json::json!(false));

        // SECOND BOOT after a user edit: the seed does not re-run (the table
        // is not empty), the edit survives, and the export is refreshed.
        db.upsert_chat_model_context(
            ChatModelContextInput { context_window: 777, ..input("glm-5.1") },
            true,
        )
        .unwrap();
        seed_and_export_on_boot(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(
            written["models"]["glm-5.1"]["context_window"],
            serde_json::json!(777),
            "a boot must never silently revert a user edit"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// No shipped seed reachable (a binary-only install, or a clone without
    /// the gateway package): boot must not fail, must seed NOTHING, and must
    /// still export — an empty export is a valid document, and writing it is
    /// what keeps the file in step after a user empties the table.
    ///
    /// The clone is pinned rather than discovered, for the reason spelled out
    /// on `reseed_with_no_shipped_seed_file_fails_loudly`.
    #[test]
    #[serial_test::serial]
    fn boot_with_no_shipped_seed_is_quiet_and_still_exports() {
        let dir = tmp_dir("noseedboot");
        let clone = clone_without_seed(&dir);
        let target = dir.join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();
        seed_and_export_on_boot(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(
            db.list_chat_model_context().unwrap().is_empty(),
            "nothing to seed from means nothing seeded — not a partial table"
        );
        assert!(target.exists(), "an empty table still produces a valid export");
        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(written["schema_version"], serde_json::json!(1));
        assert!(written["models"].as_object().unwrap().is_empty());
        std::fs::remove_dir_all(&dir).ok();
    }

    /// A DAMAGED shipped seed is an error with a path in it, not a silent
    /// skip: the file ships with the code, so a bad one is a broken install.
    #[test]
    fn a_damaged_shipped_seed_is_reported_and_seeds_nothing() {
        let dir = tmp_dir("badseed");
        let clone = dir.join("clone");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        // `resolve_orchestrator_root`'s cached-path branch also requires
        // `check_install_status` to pass, which wants an install manifest
        // (or a `.venv/`). Write the manifest — the cheaper of the two.
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(seed_dir.join("chat_model_context.seed.json"), "{ oops").unwrap();

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();

        let err = load_seed_rows(&db).expect_err("a broken seed must be an error");
        assert!(err.contains("parse shipped seed"), "got: {}", err);
        assert!(err.contains("chat_model_context.seed.json"), "must name the file: {}", err);
        assert!(db.list_chat_model_context().unwrap().is_empty());
        std::fs::remove_dir_all(&dir).ok();
    }

    /// An uncited row in the seed fails the LOAD — R10 enforced on the way
    /// in, so it can never reach the table or the export.
    #[test]
    fn an_uncited_seed_row_fails_the_load() {
        let dir = tmp_dir("uncitedseed");
        let clone = dir.join("clone");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        // `resolve_orchestrator_root`'s cached-path branch also requires
        // `check_install_status` to pass, which wants an install manifest
        // (or a `.venv/`). Write the manifest — the cheaper of the two.
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(
            seed_dir.join("chat_model_context.seed.json"),
            r#"{"schema_version": 1, "models": {"guessed": {
                 "vendor": "zai", "context_window": 1000000,
                 "max_output": 128000, "window_1m": true, "source": ""}}}"#,
        )
        .unwrap();

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();
        let err = load_seed_rows(&db).expect_err("uncited seed row must fail");
        assert!(err.contains("source citation is required"), "got: {}", err);
        std::fs::remove_dir_all(&dir).ok();
    }
}
